import os
from typing import Optional

import torch as t
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
from days.w3d1.gptj_parallel import *
from days.w3d1.imdb_data import DistributedIMDBData

COMPONENT_PATHS = [f".data/gptj_components/component{i}.pt" for i in range(7)]
DEVICES = [f"cuda:{i}" for i in range(len(COMPONENT_PATHS))]
TRAINING_DATA_X_PATH = "imdb_train_xs_128.pt"
TRAINING_DATA_Y_PATH = "imdb_train_ys.pt"
TRAINING_DATA_SIZE = 25_000
SEQUENCE_LENGTH = 128
BATCH_SIZE = 2
PROCESS_GROUPS = {}


def get_device() -> str:
    return DEVICES[dist.get_rank()]


def get_process_group(*ranks):
    return PROCESS_GROUPS[tuple(sorted(ranks))]


def add_process_group(*ranks: int):
    ranks = tuple(sorted(ranks))
    if ranks not in PROCESS_GROUPS:
        PROCESS_GROUPS[ranks] = dist.new_group(ranks)


def _send_receive_tensor(my_tensor: Optional[t.Tensor], src: int, dst: int) -> t.Tensor:
    """Should only be called by the src and dst processes."""

    rank = dist.get_rank()
    # print(f"{rank}  Entered send_tensor({src} -> {dst})")
    assert rank in (src, dst)

    # Broadcast my_tensor metadata from src to dst
    # print(f"{rank} attempting to broadcast metadata... ({src} -> {dst})")
    metadata = [None, None] if rank == dst else [my_tensor.shape, my_tensor.dtype]
    dist.broadcast_object_list(metadata, src=src, group=get_process_group(src, dst))
    # print(f"{rank} broadcasted metadata {metadata} ({src} -> {dst})")

    shape, dtype = metadata
    if rank == dst:
        my_tensor = t.empty(shape, dtype=dtype, device=get_device())

    dist.broadcast(my_tensor, src, get_process_group(src, dst))
    return my_tensor


def send_tensor(x: t.Tensor, src: int, dst: int):
    _send_receive_tensor(x, src, dst)


def receive_tensor(src: int, dst: int) -> t.Tensor:
    return _send_receive_tensor(None, src, dst)


def forward_and_back(
    batch_x: Optional[t.Tensor],
    batch_y: Optional[t.Tensor],
    model_part: nn.Module,
) -> Optional[t.Tensor]:
    """
    pass in batch_x and batch_y to rank 0
    rank -1 returns the loss
    """
    rank = dist.get_rank()
    size = dist.get_world_size()

    # Forward pass
    if rank == 0:
        prv_activations = batch_x
    else:
        prv_activations = t.autograd.Variable(
            receive_tensor(rank - 1, rank),
            requires_grad=True,
        )

    activations = model_part(prv_activations)

    if rank < size - 1:
        send_tensor(activations, rank, rank + 1)

    if rank == 0:
        send_tensor(batch_y, src=0, dst=size - 1)
    if rank == size - 1:
        batch_y = receive_tensor(src=0, dst=size - 1)

    loss = None
    if rank == size - 1:
        logits = model_part(prv_activations)
        loss = F.cross_entropy(input=logits, target=batch_y)
        loss.backward()

    if rank < size - 1:
        activations_grad = receive_tensor(src=rank + 1, dst=rank)
        # print(f"Begin backward call at rank {rank}, {activations_grad.shape}")
        # activations = t.autograd.Variable(activations, requires_grad=True)
        # activations.backward(inputs=activations_grad)

        # TODO: Make this not so hacky...
        (activations.flatten() @ activations_grad.flatten()).backward()
        # print(f"Finished backward call at rank {rank}")

    if rank > 0:
        send_tensor(prv_activations.grad, rank, rank - 1)

    return loss


def run():
    rank = dist.get_rank()
    print(f"Beginning loop for rank {rank}")

    model_part: GPTJComponent = t.load(COMPONENT_PATHS[rank]).to(get_device())
    optimizer = t.optim.SGD(model_part.parameters(), lr=1e-3)

    train_dl = DistributedIMDBData(batch_size=BATCH_SIZE, device=get_device())
    for batch_x, batch_y in train_dl:
        forward_and_back(
            batch_x=batch_x,
            batch_y=batch_y,
            model_part=model_part,
        )

        # print(f"Start optimizer step at {rank}")
        optimizer.step()
        # print(f"End optimizer at step {rank}")


def init_process(rank, size, fn, backend="gloo"):  # TODO NCCL
    """Initialize the distributed environment."""
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = "29499"
    dist.init_process_group(backend, rank=rank, world_size=size)
    for i in range(size - 1):
        add_process_group(i, i + 1)

    fn()


if __name__ == "__main__":
    size = len(COMPONENT_PATHS)
    processes = []
    mp.set_start_method("spawn")
    for rank in range(size):
        p = mp.Process(target=init_process, args=(rank, size, run))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()
