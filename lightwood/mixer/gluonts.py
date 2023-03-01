import importlib
from copy import deepcopy
from typing import Dict, Union, Optional

import numpy as np
import pandas as pd
import mxnet as mx

from gluonts.dataset.pandas import PandasDataset

from gluonts.mx import DeepAREstimator, Trainer  # @TODO: support for other estimators
from gluonts.mx.trainer.callback import TrainingHistory
from gluonts.mx.distribution.student_t import StudentTOutput

from lightwood.helpers.log import log
from lightwood.helpers.ts import get_group_matches
from lightwood.mixer.base import BaseMixer
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class GluonTSMixer(BaseMixer):
    horizon: int
    target: str
    supports_proba: bool

    def __init__(
            self,
            stop_after: float,
            target: str,
            horizon: int,
            window: int,
            dtype_dict: Dict,
            ts_analysis: Dict,
            n_epochs: int = 10,
            early_stop_patience: int = 3,
            distribution_output: str = '',
            seed: int = 0,
            static_features_cat: Optional[list[str]] = None,
            static_features_real: Optional[list[str]] = None,
    ):
        """
        Wrapper around GluonTS probabilistic deep learning models. For now, only DeepAR is supported.

        Due to inference speed, predictions are only generated for the last data point (as opposed to other mixers).  

        :param stop_after: time budget in seconds.
        :param target: column to forecast.
        :param horizon: length of forecasted horizon.
        :param window: length of input data.
        :param dtype_dict: data type of each column in the dataset.
        :param ts_analysis: dictionary with miscellaneous time series info, as generated by 'lightwood.data.timeseries_analyzer'.
        :param n_epochs: amount of epochs to train the model for. Will perform early stopping automatically if validation loss degrades.
        :param early_stop_patience: amount of consecutive epochs with no improvement in the validation loss.
        :param distribution_output: specify the type of distribution the model will learn.
        :param seed: specifies the seed used internally by GluonTS for reproducible predictions
        """  # noqa
        super().__init__(stop_after)
        self.stable = True
        self.prepared = False
        self.supports_proba = False
        self.target = target
        self.window = window
        self.horizon = horizon
        self.n_epochs = n_epochs
        self.dtype_dict = dtype_dict
        self.ts_analysis = ts_analysis
        self.grouped_by = ['__default'] if not ts_analysis['tss'].group_by else ts_analysis['tss'].group_by
        self.groups = []  # list can grow using adjust() with new data
        self.estimator = None
        self.model = None
        self.train_cache = None
        self.patience = early_stop_patience
        self.seed = seed
        self.trains_once = True
        self.static_features_cat = static_features_cat if static_features_cat else []
        self.static_features_real = static_features_real if static_features_real else []

        dist_module = importlib.import_module('.'.join(['gluonts.mx.distribution',
                                                        *distribution_output.split(".")[:-1]]))
        try:
            self.distribution = getattr(dist_module, distribution_output.split(".")[-1])()
        except AttributeError:
            self.distribution = StudentTOutput()  # use StudentTOutput when the provided distribution does not exist

        if len(self.grouped_by) > 1:
            raise Exception("This mixer can only be used with 0 or 1 partition columns.")

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """ Fits the model. """  # noqa
        log.info('Started fitting GluonTS forecasting model')

        # prepare data
        cat_ds = ConcatedEncodedDs([train_data, dev_data])
        fit_groups = list(cat_ds.data_frame[self.grouped_by[0]].unique()) if self.grouped_by != ['__default'] else None
        train_ds = self._make_initial_ds(cat_ds.data_frame, phase='train', groups=fit_groups)
        batch_size = 32
        self.model_train_stats = TrainingHistory()

        self.estimator = DeepAREstimator(
            freq=train_ds.freq,
            prediction_length=self.horizon,
            distr_output=self.distribution,
            lags_seq=[i + 1 for i in range(self.window)],
            batch_size=batch_size,
            use_feat_static_cat=True if self.static_features_cat else False,
            use_feat_static_real=True if self.static_features_real else False,
            trainer=Trainer(
                epochs=self.n_epochs,
                num_batches_per_epoch=max(1, len(cat_ds.data_frame) // batch_size),
                callbacks=[EarlyStop(patience=self.patience), self.model_train_stats])
        )
        self.model = self.estimator.train(train_ds)
        self.prepared = True
        log.info('Successfully trained GluonTS forecasting model.')

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs,
                    args: Optional[dict] = None) -> None:
        """
        Due to how lightwood implements the `update` procedure, expected inputs for this method are:

        :param dev_data: original `test` split (used to validate and select model if ensemble is `BestOf`).
        :param train_data: concatenated original `train` and `dev` splits.
        :param args: optional arguments to customize the finetuning process.
        """  # noqa

        # handle args
        if args is None:
            args = {}
        if args.get('n_epochs'):
            self.n_epochs = args['n_epochs']
        if args.get('patience'):
            self.patience = args['patience']
        self.estimator.trainer = Trainer(epochs=self.n_epochs, callbacks=[EarlyStop(patience=self.patience)])

        # prepare data and fine-tune
        ds = ConcatedEncodedDs([train_data, dev_data])
        adjust_groups = list(ds.data_frame[self.grouped_by[0]].unique()) if self.grouped_by != ['__default'] else None
        ds = self._make_initial_ds(ds.data_frame, phase='adjust', groups=adjust_groups)
        self.model = self.estimator.train_from(self.model, ds)

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        """ 
        Calls the mixer to emit forecasts.
        """  # noqa
        mx.random.seed(self.seed)
        np.random.seed(self.seed)
        length = sum(ds.encoded_ds_lengths) if isinstance(ds, ConcatedEncodedDs) else len(ds)

        ydf = pd.DataFrame(index=np.arange(length), dtype=object)
        init_arr = [0 for _ in range(self.ts_analysis['tss'].horizon)]
        for col in ['prediction', 'lower', 'upper']:
            ydf.at[:, col] = [init_arr for _ in range(len(ydf))]

        ydf['index'] = ds.data_frame.index
        conf = args.fixed_confidence if args.fixed_confidence else 0.9
        ydf['confidence'] = conf

        gby = self.ts_analysis["tss"].group_by if self.ts_analysis["tss"].group_by else []
        groups = ds.data_frame[gby[0]].unique().tolist() if gby else None

        df = ds.data_frame
        ydf['__original_index'] = df['__mdb_original_index'].values
        input_ds = self._make_initial_ds(df, groups=groups)  # TODO test with novel group
        forecasts = list(self.model.predict(input_ds))
        for group, group_forecast in zip(groups, forecasts):
            _, subdf = get_group_matches(df, (group, ), gby)
            idx = ydf[ydf['__original_index'] == max(subdf['__mdb_original_index'])].index.values[0]
            ydf.at[idx, 'prediction'] = [entry for entry in group_forecast.quantile(0.5)]
            ydf.at[idx, 'lower'] = [entry for entry in group_forecast.quantile(1 - conf)]
            ydf.at[idx, 'upper'] = [entry for entry in group_forecast.quantile(conf)]

        return ydf

    def _make_initial_ds(self, df=None, phase='predict', groups=None):
        oby_col_name = '__gluon_timestamp'
        gby = self.ts_analysis["tss"].group_by if self.ts_analysis["tss"].group_by else []
        freq = self.ts_analysis['sample_freqs']['__default']
        keep_cols = [self.target] + [col for col in gby] + self.static_features_cat + self.static_features_real

        agg_map = {self.target: 'sum'}
        for col in self.static_features_cat:
            agg_map[col] = 'first'
        for col in self.static_features_real:
            agg_map[col] = 'mean'

        if groups is None and gby:
            groups = self.groups
        elif gby and phase in ('train', 'adjust') and self.grouped_by != ['__default']:
            # we extend all seen groups for subsequent adjustments
            self.groups.extend(set(groups))

        if df is None and phase not in ('train', 'adjust'):
            df = self.train_cache
            if gby:
                df = df[df[gby[0]].isin(groups)]
        else:
            sub_df = df[keep_cols]
            df = deepcopy(sub_df)

            if phase == 'train':
                self.train_cache = df.sort_index()
            else:
                if gby:
                    cache = self.train_cache[self.train_cache[gby[0]].isin(groups)]
                else:
                    cache = self.train_cache

                if phase == 'adjust':
                    # update cache to include all new information (pre-group filter)
                    self.train_cache = pd.concat([self.train_cache, df]).drop_duplicates().sort_index()

                df = pd.concat([cache, df]).sort_index()

        df = df.drop_duplicates()

        if len(df) == 0:
            return None

        if gby:
            # @TODO: multiple group support and remove groups without enough data
            df = df.groupby(by=gby[0]).resample(freq).agg(agg_map).reset_index(level=[0])
        else:
            df = df.resample(freq).agg(agg_map)
            gby = '__default_group'
            df[gby] = '__default_group'

        df[oby_col_name] = df.index
        ds = PandasDataset.from_long_dataframe(
            df,
            target=self.target,
            item_id=gby,
            freq=freq,
            timestamp=oby_col_name,
            # feat_dynamic_real=None,
            # feat_dynamic_cat=None,
            feat_static_real=self.static_features_real if self.static_features_real else None,
            feat_static_cat=self.static_features_cat if self.static_features_cat else None,
        )
        return ds


class EarlyStop(TrainingHistory):
    def __init__(self, patience=3):
        super().__init__()
        self.patience = max(1, patience)
        self.counter = 0

    def on_validation_epoch_end(
        self,
        epoch_no: int,
        epoch_loss: float,
        training_network,
        trainer,
    ) -> bool:
        super().on_validation_epoch_end(epoch_no, epoch_loss, training_network, trainer)

        if len(self.validation_loss_history) > 1:
            if self.validation_loss_history[-1] > self.validation_loss_history[-2]:
                self.counter += 1
            else:
                self.counter = 0  # reset if not successive

        if self.counter >= self.patience:
            return False
        else:
            return True
