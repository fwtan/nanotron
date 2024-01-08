import json
from pathlib import Path
from typing import Optional

import torch

from nanotron.core import optim as optim
from nanotron.distributed import ParallelContext, ParallelMode
from nanotron.serialize.utils import ObjectType


def optimizer_filename(parallel_context: ParallelContext, is_zero: bool):
    dp_rank = parallel_context.get_local_rank(ParallelMode.DATA)
    pp_rank = parallel_context.get_local_rank(ParallelMode.PIPELINE)

    dp_world_size = parallel_context.get_world_size(ParallelMode.DATA)
    pp_world_size = parallel_context.get_world_size(ParallelMode.PIPELINE)

    if is_zero is True:
        tp_rank = parallel_context.get_local_rank(ParallelMode.TENSOR)
        tp_world_size = parallel_context.get_world_size(ParallelMode.TENSOR)
        return f"{ObjectType.OPTIMIZER.value}_pp-{pp_rank}-of-{pp_world_size}_dp-{dp_rank}-of-{dp_world_size}_tp-{tp_rank}-of-{tp_world_size}.pt"
    else:
        return f"{ObjectType.OPTIMIZER.value}_pp-{pp_rank}-of-{pp_world_size}_tp-{dp_rank}-of-{dp_world_size}.pt"


def lr_scheduler_filename():
    """The lr_scheduler is the same for all processes."""
    return f"{ObjectType.LR_SCHEDULER.value}.pt"


def save_optimizer(
    optimizer: optim.BaseOptimizer,
    parallel_context: ParallelContext,
    root_folder: Path,
):
    """Saves optimizer states
    - If Zero-0 is used, optimizer states are replicated across all DPs. Only DP-0 saves the states
    - If Zero-1 is used, optimizer states are sharded across all DPs. Each DP saves its own states
    """
    # TODO @thomasw21: Figure out if I need to save param groups. Right now I'm assuming no as we only store what's trainable
    # TODO @thomasw21: We can probably "rotate" so that every process stores something (maybe doesn't matter if we're I/O bound)
    root_folder = root_folder / "optimizer"
    root_folder.mkdir(exist_ok=True, parents=True)

    if parallel_context.get_world_size(ParallelMode.GLOBAL) == 0:
        with open(root_folder / "optimizer_config.json", "w") as fo:
            json.dump({"type": optimizer.__class__.__name__}, fo)

    dp_rank = parallel_context.get_local_rank(ParallelMode.DATA)
    if (not optimizer.inherit_from(optim.ZeroDistributedOptimizer)) and dp_rank > 0:
        # this is Zero-0, so only DP-0 saves the optimizer states
        return

    # We dump the optimizer state using `torch.save`
    torch.save(
        optimizer.state_dict(),
        root_folder
        / optimizer_filename(parallel_context, is_zero=optimizer.inherit_from(optim.ZeroDistributedOptimizer)),
    )


def save_lr_scheduler(
    lr_scheduler,
    parallel_context: ParallelContext,
    root_folder: Path,
):
    """Saves lr scheduler states"""
    if parallel_context.get_global_rank() > 0:
        # Only WORLD-RANK 0 saves the lr scheduler state
        return

    root_folder = root_folder / "lr_scheduler"
    root_folder.mkdir(exist_ok=True, parents=True)

    # We dump the optimizer state using `torch.save`
    torch.save(
        lr_scheduler.state_dict(),
        root_folder / lr_scheduler_filename(),
    )


def load_optimizer(
    optimizer: optim.BaseOptimizer,
    parallel_context: ParallelContext,
    root_folder: Path,
    map_location: Optional[str] = None,
):
    root_folder = root_folder / "optimizer"
    # `load_state_dict` copies the state dict which can be very large in case of Zero-0 so we load to cpu and then move to the right device
    map_location = "cpu" if not optimizer.inherit_from(optim.ZeroDistributedOptimizer) else map_location

    # TODO @thomasw21: Load optimizer type and check that it's compatible otherwise we might be be loading something else completely
    state_dict = torch.load(
        root_folder
        / optimizer_filename(parallel_context, is_zero=optimizer.inherit_from(optim.ZeroDistributedOptimizer)),
        map_location=map_location,
    )
    optimizer.load_state_dict(state_dict)


def load_lr_scheduler(
    lr_scheduler,
    root_folder: Path,
):
    root_folder = root_folder / "lr_scheduler"

    state_dict = torch.load(root_folder / lr_scheduler_filename())
    lr_scheduler.load_state_dict(state_dict)
