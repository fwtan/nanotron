import pytest
import torch
from datasets import load_dataset
from helpers.utils import init_distributed
from nanotron import distributed as dist
from nanotron.doremi.dataloader import DistributedSamplerForDoReMi
from nanotron.doremi.doremi_context import DoReMiContext
from nanotron.parallel import ParallelContext


@pytest.fixture
def dataset1():
    return load_dataset("stas/c4-en-10k", split="train")


@pytest.mark.parametrize(
    "domain_weights",
    [
        # NOTE: test auto fill samples if there are rounding errors
        torch.tensor([0.296, 0.201, 0.501]),
        # NOTE: if sampling based on batch size, then
        # the last domain results in no sample (round(0.004 * 64) = 0)
        # but if do with global batch size, (round(0.004 * 512) = 2)
        torch.tensor([0.498, 0.498, 0.004]),
        torch.tensor(
            [
                0.34356916553540745,
                0.16838812972610234,
                0.24711766854236725,
                0.0679225638705455,
                0.059079828519653675,
                0.043720261601881555,
                0.01653850841342608,
                0.00604146633842096,
                0.04342813428189645,
                0.0041942731702987,
            ]
        ),
    ],
)
@pytest.mark.parametrize("dp_size", [1, 2, 4])
def test_sampling_from_dist_doremi_sampler_with_global_batch_size(dp_size, domain_weights: torch.Tensor, dataset1):
    global_batch_size = 512
    num_microbatches = 32
    # batch_size = 4
    # dp_size = global_batch_size // (batch_size * num_microbatches)
    batch_size = global_batch_size // (num_microbatches * dp_size)
    datasets = [dataset1 for _ in range(len(domain_weights))]
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=1, dp=dp_size, pp=1)(_test_sampling_from_dist_doremi_sampler_with_global_batch_size)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        global_batch_size=global_batch_size,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_sampling_from_dist_doremi_sampler_with_global_batch_size(
    parallel_context: ParallelContext,
    batch_size: int,
    num_microbatches: int,
    global_batch_size: int,
    datasets,
    doremi_context: DoReMiContext,
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    domain_weights = doremi_context.domain_weights
    global_batch_size_per_domain = [round(global_batch_size * weight.item()) for weight in domain_weights]

    microbatch_idx = 0
    num_samples_per_domain = [0 for _ in range(len(domain_weights))]
    yielded_idxs = []
    for idxs in sampler:
        assert batch_size == len(idxs)

        # NOTE: make sure the indicies from a batch
        # is proportion to the domain weights
        start_indices = [sum([len(ds) for ds in datasets[:i]]) for i in range(len(datasets))]
        end_indices = [sum([len(ds) for ds in datasets[: i + 1]]) for i in range(len(datasets))]
        for domain_idx in range(len(domain_weights)):
            num_samples = sum(1 for idx in idxs if idx >= start_indices[domain_idx] and idx < end_indices[domain_idx])
            num_samples_per_domain[domain_idx] += num_samples

        if microbatch_idx == num_microbatches - 1:
            # NOTE: if this is the last microbatch => we iterate through all the microbatches
            # now we check if the overall number of samples in each domain is correct across
            # all the microbatches
            num_samples_per_domain = torch.tensor(num_samples_per_domain, dtype=torch.int, device="cuda")

            # NOTE: the domain weights are chosen so that we expect
            # no domains have zero sample in the global batch size
            dist.all_reduce(num_samples_per_domain, op=dist.ReduceOp.SUM)
            assert (num_samples_per_domain == 0).sum().item() == 0

            for expected_bs, bs in zip(global_batch_size_per_domain, num_samples_per_domain):
                # NOTE: take into account rounding errors
                assert abs(expected_bs - bs) <= 1, f"abs(expected_bs - bs): {abs(expected_bs - bs)}"

            microbatch_idx = 0
            num_samples_per_domain = [0 for _ in range(len(domain_weights))]
            continue

        microbatch_idx += 1
        yielded_idxs.extend(idxs)

    total_yielded_idxs = torch.tensor(len(yielded_idxs), dtype=torch.int, device="cuda")
    total_samples = sum([round(len(ds) * weight.item()) for ds, weight in zip(datasets, domain_weights)])
    dist.all_reduce(total_yielded_idxs, op=dist.ReduceOp.SUM)
    assert (
        total_yielded_idxs == total_samples
    ), f"total_yielded_idxs: {total_yielded_idxs}, total_samples: {total_samples}"


@pytest.mark.parametrize("dp_size", [1, 2, 4])
def test_dist_doremi_sampler_not_repeating_samples(dp_size, dataset1):
    global_batch_size = 512
    num_microbatches = 32
    # batch_size = 4
    # dp_size = global_batch_size // (batch_size * num_microbatches)
    batch_size = global_batch_size // (num_microbatches * dp_size)
    domain_weights = torch.tensor([0.296, 0.201, 0.501])
    datasets = [dataset1 for _ in range(len(domain_weights))]
    domain_keys = [f"domain {i}" for i in range(len(datasets))]
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    init_distributed(tp=1, dp=dp_size, pp=1)(_test_dist_doremi_sampler_not_repeating_samples)(
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        datasets=datasets,
        doremi_context=doremi_context,
    )


def _test_dist_doremi_sampler_not_repeating_samples(
    parallel_context: ParallelContext,
    batch_size: int,
    num_microbatches: int,
    datasets,
    doremi_context: DoReMiContext,
):
    dp_size = dist.get_world_size(parallel_context.dp_pg)
    dp_rank = dist.get_rank(parallel_context.dp_pg)

    sampler = DistributedSamplerForDoReMi(
        datasets,
        batch_size=batch_size,
        num_microbatches=num_microbatches,
        num_replicas=dp_size,
        rank=dp_rank,
        doremi_context=doremi_context,
        parallel_context=parallel_context,
    )

    microbatch_idx = 0
    yielded_idxs = []
    for idxs in sampler:
        # NOTE: check that the indicies are not repeated
        assert not set(idxs).intersection(
            yielded_idxs
        ), f"microbatch_idx: {microbatch_idx}, yielded_idxs: {yielded_idxs}, idxs: {idxs}"

        microbatch_idx += 1
        yielded_idxs.extend(idxs)
