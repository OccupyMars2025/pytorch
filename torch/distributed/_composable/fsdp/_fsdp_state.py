import functools

from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.autograd.variable import queue_callback
from torch.autograd.graph import Node, register_multi_grad_hook
from torch.distributed._composable_state import (
    _get_module_state,
    _insert_module_state,
    _State,
)
from torch.distributed.utils import _to_kwargs
from torch.utils._pytree import tree_flatten, tree_map
from torch.utils.hooks import RemovableHandle
from ._fsdp_api import MixedPrecisionPolicy
from ._fsdp_common import _cast_fp_tensor, TrainingState
from ._fsdp_param import FSDPParam
from ._fsdp_param_group import FSDPCommContext, FSDPParamGroup


class FSDPStateContext:
    """This has state shared across FSDP states."""

    def __init__(self):
        # All FSDP states in the root state's module tree
        self.all_states: List[FSDPState] = []
        # Iteration's forward root runs the once-per-forward logic; this root
        # may not be the overall root set by lazy initialization in cases where
        # only a submodule runs forward (e.g. encoder-only for eval)
        self.iter_forward_root: Optional[FSDPState] = None
        # Final callback should only be queued once per backward
        self.post_backward_final_callback_queued: bool = False
        # Whether to finalize backward in this backward's final callback
        self.is_last_backward: bool = True


def _fsdp_state_pre_forward(
    self, module: nn.Module, args: Tuple[Any, ...], kwargs: Dict[str, Any]
) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    # When composing with module-hook-based activation checkpointing, the
    # the pre-backward hook is responsible for the unshard
    if self._training_state == TrainingState.PRE_BACKWARD:
        return args, kwargs
    self._training_state = TrainingState.FORWARD
    args, kwargs = self._root_pre_forward(module, args, kwargs)
    if self._mp_policy.cast_forward_inputs and self._mp_policy.param_dtype:
        with torch.profiler.record_function("FSDP::cast_forward_inputs"):
            cast_fn = functools.partial(
                _cast_fp_tensor, self._mp_policy.param_dtype
            )
            args, kwargs = tree_map(cast_fn, args), tree_map(cast_fn, kwargs)
    if self._fsdp_param_group:
        args, kwargs = self._fsdp_param_group.pre_forward(module, args, kwargs)
    return args, kwargs

def _fsdp_state_post_forward(self, module: nn.Module, input: Any, output: Any) -> Any:
    # When composing with module-hook-based activation checkpointing, the
    # post-backward hook is responsible for the reshard
    if self._training_state == TrainingState.PRE_BACKWARD:
        return output
    if self._fsdp_param_group:
        output = self._fsdp_param_group.post_forward(module, input, output)
    output = self._register_pre_backward_hook(output)
    self._training_state = TrainingState.IDLE
    if self._state_ctx.iter_forward_root is self:
        if all_gather_state := self._comm_ctx.all_gather_state:
            if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
                # Free the last all-gather result if needed; refer to
                # [Note: Overlapping all-gather copy-in and all-gather]
                self._comm_ctx.all_gather_copy_in_stream.wait_event(
                    all_gather_state.event
                )
                self._comm_ctx.all_gather_stream.wait_event(all_gather_state.event)
            self._comm_ctx.all_gather_state = None  # free the all-gather result
        self._state_ctx.iter_forward_root = None
    if self._mp_policy.output_dtype is not None:
        with torch.profiler.record_function("FSDP::cast_forward_outputs"):
            output = tree_map(
                functools.partial(_cast_fp_tensor, self._mp_policy.output_dtype),
                output,
            )
    return output

def _fsdp_state_pre_backward(self, forward_grad_fns: Tuple[Node, ...], grad) -> None:
    """
    NOTE(yf225): since under compile we use `register_hook` to call pre_backward, to mimic `multi_grad_hook` "any" mode behavior
    we only want to call pre_backward once, so doing this check here to early return if already called.
    
    Comment from Andrew:
    one more thing to note is that the hook should run once per call to register_multi_grad_hook, where there is one call per forward
    so if we run multiple forward before backward, we should run the pre-backward hook multiple times (one per forward)
    as such, the bool to guard whether the pre-backward hook is a no-op or not needs to be per call to register_multi_grad_hook, not something global to the entire backward
    """
    if self._training_state == TrainingState.PRE_BACKWARD:
        return
    self._training_state = TrainingState.PRE_BACKWARD
    self._register_root_post_backward_final_callback()
    if self._fsdp_param_group:
        self._fsdp_param_group.pre_backward(forward_grad_fns)
    # NOTE(yf225): this is only needed because we are using `register_hook`. Not needed if we use `register_multi_grad_hook`.
    return grad

def _fsdp_state_root_post_backward_final_callback(self, *unused) -> None:
    with torch.profiler.record_function("FSDP::root_post_backward_callback"):
        for state in self._state_ctx.all_states:
            if state._fsdp_param_group and state._fsdp_param_group.is_unsharded:
                # Run post-backward in case forward inputs did not require
                # gradient so the autograd backward did not run
                state._fsdp_param_group.post_backward()
            if self._state_ctx.is_last_backward:
                state._finalize_backward()
        if self._state_ctx.is_last_backward:
            self._comm_ctx.post_forward_order.clear()
        self._state_ctx.post_backward_final_callback_queued = False


class FSDPState(_State):
    def __init__(self):
        super().__init__()
        self._fsdp_param_group: Optional[FSDPParamGroup] = None
        self._is_root: Optional[bool] = None  # root set during lazy init
        self._state_ctx = FSDPStateContext()
        self._comm_ctx = FSDPCommContext()
        self._training_state: TrainingState = TrainingState.IDLE
        self._pre_backward_hook_handles: List[RemovableHandle] = []

    # Define a separate init since `__init__` is called in the contract
    def init(
        self, module: nn.Module, device: torch.device, mp_policy: MixedPrecisionPolicy
    ) -> None:
        _insert_module_state(module, self)
        self._module = module
        self._device = device
        self._mp_policy = mp_policy
        self._pre_forward_hook_handle = module.register_forward_pre_hook(
            functools.partial(_fsdp_state_pre_forward, self), prepend=True, with_kwargs=True
        )
        self._post_forward_hook_handle = module.register_forward_hook(
            functools.partial(_fsdp_state_post_forward, self), prepend=False
        )

    def _root_pre_forward(
        self, module: nn.Module, args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        self._lazy_init()
        if self._state_ctx.iter_forward_root is not None:
            return args, kwargs
        self._state_ctx.iter_forward_root = self
        with torch.profiler.record_function("FSDP::root_pre_forward"):
            if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
                # Wait for optimizer before implicitly prefetched all-gathers
                current_stream = torch.cuda.current_stream()
                self._comm_ctx.all_gather_copy_in_stream.wait_stream(current_stream)
                self._comm_ctx.all_gather_stream.wait_stream(current_stream)
            if self._device.type == "cuda":
                with torch.profiler.record_function("FSDP::inputs_to_device"):
                    args_tuple, kwargs_tuple = _to_kwargs(
                        args, kwargs, self._device, False
                    )  # same as DDP
                args, kwargs = args_tuple[0], kwargs_tuple[0]
        return args, kwargs

    def _lazy_init(self) -> None:
        """
        Lazy initialization represents when all modules' parallelisms have
        finalized (e.g. FSDP has been applied to all desired modules). This
        means that we can determine which state is the root, and we do so by
        the 1st state to run forward.
        """
        if self._is_root is not None:
            return  # no-op: already initialized
        self._is_root = True
        root_module = self._module
        for module_name, module in root_module.named_modules():
            if (state := _get_module_fsdp_state(module)) is None:
                continue
            if module is not root_module:
                if state._is_root is not None:
                    raise RuntimeError(
                        "FSDP state has already been lazily initialized for "
                        f"{module_name}\nFSDP requires running forward through "
                        "the root module first"
                    )
                state._is_root = False
            self._state_ctx.all_states.append(state)
            if state._fsdp_param_group:
                state._fsdp_param_group.lazy_init()
        if self._fsdp_param_group:
            # For the root, do not reshard after forward since for training,
            # the parameters would be freed and all-gathered immediately
            self._fsdp_param_group.post_forward_mesh_info = None
        self._init_fqns()
        self._init_shared_state()

    def _init_shared_state(self) -> None:
        self._comm_ctx.init()
        for state in self._state_ctx.all_states:
            state._state_ctx = self._state_ctx
            state._comm_ctx = self._comm_ctx
            if fsdp_param_group := state._fsdp_param_group:
                fsdp_param_group.comm_ctx = self._comm_ctx

    def _init_fqns(self) -> None:
        """Sets module and parameter FQN attributes for debugging."""
        assert self._is_root
        root_module = self._module
        param_to_fsdp_param: Dict[nn.Parameter, FSDPParam] = {}
        module_to_fsdp_param_group: Dict[nn.Module, FSDPParamGroup] = {}
        for state in self._state_ctx.all_states:
            if fsdp_param_group := state._fsdp_param_group:
                for fsdp_param in fsdp_param_group.fsdp_params:
                    param_to_fsdp_param[fsdp_param.sharded_param] = fsdp_param
                module_to_fsdp_param_group[fsdp_param_group.module] = fsdp_param_group
        for param_name, param in root_module.named_parameters():
            if param in param_to_fsdp_param:
                param_to_fsdp_param[param]._param_fqn = param_name
        for module_name, module in root_module.named_modules():
            if module in module_to_fsdp_param_group:
                module_to_fsdp_param_group[module]._module_fqn = module_name

    def _finalize_backward(self) -> None:
        self._training_state = TrainingState.IDLE
        if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
            for handle in self._pre_backward_hook_handles:
                handle.remove()
        self._pre_backward_hook_handles.clear()
        if self._fsdp_param_group:
            self._fsdp_param_group.finalize_backward()

    def _register_pre_backward_hook(self, output: Any) -> Any:
        if not torch.is_grad_enabled():
            return output

        flat_outputs, _ = tree_flatten(output)
        tensors = tuple(t for t in flat_outputs if t.requires_grad)
        if tensors:
            # NOTE(yf225): unfortunately `t.grad_fn` is not supported by Dynamo yet, so we set grad_fns = [] here
            # and unconditionally do unshard in `_prefetch_unshard()`
            if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
                grad_fns = tuple(t.grad_fn for t in tensors if t.grad_fn is not None)
            else:
                grad_fns = []
            pre_backward = functools.partial(_fsdp_state_pre_backward, self, grad_fns)
            # handle = register_multi_grad_hook(tensors, pre_backward, mode="any")
            # self._pre_backward_hook_handles.append(handle)
            for tensor in tensors:
                handle = tensor.register_hook(pre_backward)
                self._pre_backward_hook_handles.append(handle)
            if self._fsdp_param_group:
                if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
                    self._fsdp_param_group.all_forward_output_grad_fns.add(grad_fns)
        return output

    def _register_root_post_backward_final_callback(self):
        if self._state_ctx.post_backward_final_callback_queued:
            return
        self._state_ctx.post_backward_final_callback_queued = True
        if not torch.distributed._functional_collectives.is_torchdynamo_compiling():
            Variable._execution_engine.queue_callback(
                functools.partial(_fsdp_state_root_post_backward_final_callback, self)
            )
        else:
            queue_callback(
                functools.partial(_fsdp_state_root_post_backward_final_callback, self)
            )


def _get_module_fsdp_state(module: nn.Module) -> Optional[FSDPState]:
    state = _get_module_state(module)
    if isinstance(state, FSDPState):
        return state
    return None
