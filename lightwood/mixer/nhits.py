from typing import Dict, Union, Optional
from copy import deepcopy

import numpy as np
import pandas as pd
from neuralforecast import NeuralForecast
from neuralforecast.models.nhits import NHITS
from neuralforecast.losses.pytorch import MQLoss

from lightwood.helpers.log import log
from lightwood.mixer.base import BaseMixer
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class NHitsMixer(BaseMixer):
    horizon: int
    target: str
    supports_proba: bool
    model_path: str
    hyperparam_search: bool
    default_config: dict

    def __init__(
            self,
            stop_after: float,
            target: str,
            horizon: int,
            window: int,
            dtype_dict: Dict,
            ts_analysis: Dict,
            pretrained: bool = False,
            train_args: Optional[Dict] = None,
    ):
        """
        Wrapper around an N-HITS deep learning model.
        
        :param stop_after: time budget in seconds.
        :param target: column to forecast.
        :param horizon: length of forecasted horizon.
        :param window: length of input data.
        :param ts_analysis: dictionary with miscellaneous time series info, as generated by 'lightwood.data.timeseries_analyzer'.
        :param train_args: arguments to steer the training process. 
            - `trainer_args`: all arguments for the PyTorchLightning trainer.
            - `conf_level`: level passed into MQLoss. Directly impacts prediction bounds. 
        """  # noqa
        super().__init__(stop_after)
        self.stable = False
        self.prepared = False
        self.supports_proba = False
        self.target = target
        self.window = window
        self.horizon = horizon
        self.dtype_dict = dtype_dict
        self.ts_analysis = ts_analysis
        self.grouped_by = ['__default'] if not ts_analysis['tss'].group_by else ts_analysis['tss'].group_by
        self.train_args = train_args.get('trainer_args', {}) if train_args else {}
        self.conf_level = self.train_args.pop('conf_level', 90)

        self.pretrained = pretrained
        self.base_url = 'https://nixtla-public.s3.amazonaws.com/transfer/pretrained_models/'
        self.freq_to_model = {
            'Y': 'yearly',
            'Q': 'monthly',
            'M': 'monthly',
            'W': 'daily',
            'D': 'daily',
            'H': 'hourly',
            'T': 'hourly',  # NOTE: use another pre-trained model once available
            'S': 'hourly'  # NOTE: use another pre-trained model once available
        }
        self.model_names = {
            'hourly': 'nhits_m4_hourly.ckpt',  # hourly (non-tiny)
            'daily': 'nhits_m4_daily.ckpt',   # daily
            'monthly': 'nhits_m4_monthly.ckpt',  # monthly
            'yearly': 'nhits_m4_yearly.ckpt',  # yearly
        }
        self.model_name = None
        self.model = None

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Fits the N-HITS model.
        """  # noqa
        log.info('Started fitting N-HITS forecasting model')

        # prepare data
        cat_ds = ConcatedEncodedDs([train_data, dev_data])
        oby_col = self.ts_analysis["tss"].order_by
        gby = self.ts_analysis["tss"].group_by if self.ts_analysis["tss"].group_by else []
        df = deepcopy(cat_ds.data_frame)
        Y_df = self._make_initial_df(df)
        if gby:
            n_time = df[gby].value_counts().min()
        else:
            n_time = len(df[f'__mdb_original_{oby_col}'].unique())
        n_ts_val = max(int(.1 * n_time), self.horizon)  # at least self.horizon to validate&test on
        n_ts_test = max(int(.1 * n_time), self.horizon)

        # train the model
        n_time_out = self.horizon
        if self.pretrained:
            # TODO: let user specify finetuning
            self.model_name = self.model_names.get(self.freq_to_model[self.ts_analysis['sample_freqs']['__default']],
                                                   None)
            self.model_name = self.model_names['hourly'] if self.model_name is None else self.model_name
            ckpt_url = self.base_url + self.model_name
            self.model = NHITS.load_from_checkpoint(ckpt_url)

            if not self.window < self.model.hparams.n_time_in:
                log.info(f'NOTE: Provided window ({self.window}) is smaller than specified model input length ({self.model.hparams.n_time_in}). Will train a new model from scratch.')  # noqa
                self.pretrained = False
            if self.horizon > self.model.hparams.n_time_out:
                log.info(f'NOTE: Horizon ({self.horizon}) is bigger than that of the pretrained model ({self.model.hparams.n_time_out}). Will train a new model from scratch.')  # noqa
                self.pretrained = False
            if self.pretrained:
                log.info(f'Successfully loaded pretrained N-HITS forecasting model ({self.model_name})')

        if not self.pretrained:
            if self.window + self.horizon > n_time:
                new_window = max(1, n_time - self.horizon - 1)
                self.window = new_window
                log.info(f'Window {self.window} is too long for data provided (group: {df[gby].value_counts()[::-1].index[0]}), reducing window to {new_window}.')  # noqa
            model = NHITS(h=n_time_out, input_size=self.window, **self.train_args, loss=MQLoss(level=[self.conf_level]))
            self.model = NeuralForecast(models=[model], freq=self.ts_analysis['sample_freqs']['__default'])
            self.model.fit(df=Y_df, val_size=n_ts_val)
            log.info('Successfully trained N-HITS forecasting model.')

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs, args: Optional[dict] = None) -> None:
        # TODO: reimplement this with automatic novel-row differential
        self.hyperparam_search = False
        self.fit(dev_data, train_data)
        self.prepared = True

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        """
        Calls the mixer to emit forecasts.
        
        NOTE: in the future we may support predicting every single row efficiently. For now, this mixer
        replicates the neuralforecast library behavior and returns a forecast strictly for the next `tss.horizon`
        timesteps after the end of the input dataframe.
        """  # noqa
        if args.predict_proba:
            log.warning('This mixer does not output probability estimates')

        length = sum(ds.encoded_ds_lengths) if isinstance(ds, ConcatedEncodedDs) else len(ds)
        ydf = pd.DataFrame(0,  # zero-filled
                           index=np.arange(length),
                           columns=['prediction', 'lower', 'upper'],
                           dtype=object)

        input_df = self._make_initial_df(deepcopy(ds.data_frame))
        ydf['index'] = input_df['index']

        pred_cols = [f'NHITS-lo-{self.conf_level}', 'NHITS-median', f'NHITS-hi-{self.conf_level}']
        target_cols = ['lower', 'prediction', 'upper']
        for target_col in target_cols:
            ydf[target_col] = [[0 for _ in range(self.horizon)] for _ in range(len(ydf))]  # zero-filled arrays

        group_ends = []
        for group in input_df['unique_id'].unique():
            group_ends.append(input_df[input_df['unique_id'] == group]['index'].iloc[-1])
        fcst = self.model.predict(futr_df=input_df).reset_index()

        for gidx, group in zip(group_ends, input_df['unique_id'].unique()):
            for pred_col, target_col in zip(pred_cols, target_cols):
                group_preds = fcst[fcst['unique_id'] == group][pred_col].tolist()[:self.horizon]
                idx = ydf[ydf['index'] == gidx].index[0]
                ydf.at[idx, target_col] = group_preds

        ydf['confidence'] = 0.9  # TODO: set through `args`
        return ydf

    def _make_initial_df(self, df):
        oby_col = self.ts_analysis["tss"].order_by
        df = df.sort_values(by=f'__mdb_original_{oby_col}')
        df[f'__mdb_parsed_{oby_col}'] = df.index
        df = df.reset_index(drop=True)

        Y_df = pd.DataFrame()
        Y_df['y'] = df[self.target]
        Y_df['ds'] = df[f'__mdb_parsed_{oby_col}']

        if self.grouped_by != ['__default']:
            Y_df['unique_id'] = df[self.grouped_by].apply(lambda x: ','.join([elt for elt in x]), axis=1)
        else:
            Y_df['unique_id'] = '__default'

        return Y_df.reset_index()
