import os
import yaml
import json
import warnings
import os.path as osp
from functools import partial
from typing import Optional, Union, Dict

import mmcv
import torch
from mmcv.runner import HOOKS
from mmcv.runner.dist_utils import master_only
from mmcv.parallel.scatter_gather import scatter
from mmcv.parallel.utils import is_module_wrapper

from .text import TextLoggerHook


@HOOKS.register_module(force=True)
class PaviLoggerHook(TextLoggerHook):

    def __init__(self,
                 init_kwargs: Optional[Dict] = None,
                 add_graph: Optional[bool] = None,
                 img_key: Optional[str] = None,
                 add_last_ckpt: bool = False,
                 interval: int = 10,
                 ignore_last: bool = True,
                 reset_flag: bool = False,
                 by_epoch: bool = True,
                 add_graph_kwargs: Optional[Dict] = None,
                 add_ckpt_kwargs: Optional[Dict] = None,
                 interval_exp_name: int = 1000,
                 out_dir: Optional[str] = None,
                 out_suffix: Union[str, tuple] = ...,
                 keep_local: bool = True,
                 ndigits: int = 4,
                 file_client_args: Optional[Dict] = None):
        super().__init__(by_epoch, interval, ignore_last, reset_flag,
                         interval_exp_name, out_dir, out_suffix, keep_local,
                         ndigits, file_client_args)

        self.init_kwargs = init_kwargs

        add_graph_kwargs = {} if add_graph_kwargs is None else add_graph_kwargs
        self.add_graph = add_graph_kwargs.get('active', False)
        self.add_graph_start = add_graph_kwargs.get('start', 0)
        self.add_graph_interval = add_graph_kwargs.get('interval', 1)
        self.img_key = add_graph_kwargs.get('img_key', 'img')
        self.opset_version = add_graph_kwargs.get('opset_version', 11)
        self.dummy_forward_kwargs = add_graph_kwargs.get(
            'dummy_forward_kwargs', {})
        if add_graph is not None:
            warnings.warn(
                '"add_graph" is deprecated in `PaviLoggerHook`, please use '
                'the key "active" of add_graph_kwargs instead',
                DeprecationWarning)
            self.add_graph = add_graph
        if img_key is not None:
            warnings.warn(
                '"img_key" is deprecated in `PaviLoggerHook`, please use '
                'the key "img_key" of add_graph_kwargs instead',
                DeprecationWarning)
            self.img_key = img_key

        add_ckpt_kwargs = {} if add_ckpt_kwargs is None else add_ckpt_kwargs
        self.add_ckpt = add_ckpt_kwargs.get('active', False)
        self.add_last_ckpt = add_last_ckpt
        self.add_ckpt_start = add_ckpt_kwargs.get('start', 0)
        self.add_ckpt_interval = add_ckpt_kwargs.get('interval', 1)

    @master_only
    def before_run(self, runner) -> None:
        super().before_run(runner)
        try:
            from pavi import SummaryWriter
        except ImportError:
            raise ImportError(
                'No module named pavi, please contact pavi team or visit'
                'document for pavi installation instructions.')

        self.run_name = runner.work_dir.split('/')[-1]

        if not self.init_kwargs:
            self.init_kwargs = dict()
        self.init_kwargs.setdefault('name', self.run_name)
        self.init_kwargs.setdefault('model', runner._model_name)
        if runner.meta is not None:
            if 'config_dict' in runner.meta:
                config_dict = runner.meta['config_dict']
                assert isinstance(
                    config_dict,
                    dict), ('meta["config_dict"] has to be of a dict, '
                            f'but got {type(config_dict)}')
            elif 'config_file' in runner.meta:
                config_file = runner.meta['config_file']
                config_dict = dict(mmcv.Config.fromfile(config_file))
            else:
                config_dict = None
            if config_dict is not None:
                # 'max_.*iter' is parsed in pavi sdk as the maximum iterations
                #  to properly set up the progress bar.
                config_dict = config_dict.copy()
                config_dict.setdefault('max_iter', runner.max_iters)
                # non-serializable values are first converted in
                # mmcv.dump to json
                config_dict = json.loads(
                    mmcv.dump(config_dict, file_format='json'))
                session_text = yaml.dump(config_dict)
                self.init_kwargs.setdefault('session_text', session_text)
        self.writer = SummaryWriter(**self.init_kwargs)

    def get_step(self, runner) -> int:
        """Get the total training step/epoch."""
        if self.get_mode(runner) == 'val' and self.by_epoch:
            return self.get_epoch(runner)
        else:
            return self.get_iter(runner)

    def _add_ckpt(self, runner, ckpt_path: str, step: int) -> None:

        if osp.islink(ckpt_path):
            ckpt_path = osp.join(runner.work_dir, os.readlink(ckpt_path))

        if osp.isfile(ckpt_path):
            self.writer.add_snapshot_file(tag=self.run_name,
                                          snapshot_file_path=ckpt_path,
                                          iteration=step)

    def _add_graph(self, runner, step: int) -> None:
        from mmcv.runner.iter_based_runner import IterLoader
        if is_module_wrapper(runner.model):
            _model = runner.model.module
        else:
            _model = runner.model
        device = next(_model.parameters()).device
        # Note that if your sampler indices is generated in init method, your
        # dataset may be one less.
        if isinstance(runner.data_loader, IterLoader):
            data = next(iter(runner.data_loader._dataloader))
        else:
            data = next(iter(runner.data_loader))
        data = scatter(data, [device.index])[0]
        img = data[self.img_key]
        with torch.no_grad():
            origin_forward = _model.forward
            if hasattr(_model, 'forward_dummy'):
                _model.forward = _model.forward_dummy
            if self.dummy_forward_kwargs:
                _model.forward = partial(_model.forward,
                                         **self.dummy_forward_kwargs)
            self.writer.add_graph(_model,
                                  img,
                                  tag=f'{self.run_name}_{step}',
                                  opset_version=self.opset_version)
            _model.forward = origin_forward

    @master_only
    def log(self, runner) -> None:
        tags = self.get_loggable_tags(runner, add_mode=False)
        if tags:
            self.writer.add_scalars(self.get_mode(runner), tags,
                                    self.get_step(runner))
        return super().log(runner)

    @master_only
    def after_run(self, runner) -> None:
        if self.add_last_ckpt:
            # using runner.epoch/iter is ok since the step has been + 1
            step = runner.epoch if self.by_epoch else runner.iter

            ckpt_path = osp.join(runner.work_dir, 'latest.pth')
            self._add_ckpt(runner, ckpt_path, step)

        # flush the buffer and send a task ending signal to Pavi
        self.writer.close()

    @master_only
    def before_train_epoch(self, runner) -> None:
        super().before_train_epoch(runner)
        if not self.by_epoch:
            return None

        step = self.get_epoch(runner)
        if (self.add_graph and step >= self.add_graph_start
                and ((step - self.add_graph_start) % self.add_graph_interval
                     == 0)):  # noqa: E129
            self._add_graph(runner, step)

    @master_only
    def before_train_iter(self, runner) -> None:
        super().before_train_iter(runner)

        if self.by_epoch:
            return None

        step = self.get_iter(runner)
        if (self.add_graph and step >= self.add_graph_start
                and ((step - self.add_graph_start) % self.add_graph_interval
                     == 0)):  # noqa: E129
            self._add_graph(runner, step)

    @master_only
    def after_train_epoch(self, runner) -> None:
        super().after_train_epoch(runner)
        # Do not use runner.epoch since it starts from 0.
        if not self.by_epoch:
            return None

        step = self.get_epoch(runner)

        if (self.add_ckpt and step >= self.add_ckpt_start
                and ((step - self.add_ckpt_start) % self.add_ckpt_interval
                     == 0)):  # noqa: E129

            ckpt_path = osp.join(runner.work_dir, f'epoch_{step}.pth')

            self._add_ckpt(runner, ckpt_path, step)

    @master_only
    def after_train_iter(self, runner) -> None:
        super().after_train_iter(runner)

        if self.by_epoch:
            return None

        step = self.get_iter(runner)

        if (self.add_ckpt and step >= self.add_ckpt_start
                and ((step - self.add_ckpt_start) % self.add_ckpt_interval
                     == 0)):  # noqa: E129

            ckpt_path = osp.join(runner.work_dir, f'iter_{step}.pth')

            self._add_ckpt(runner, ckpt_path, step)