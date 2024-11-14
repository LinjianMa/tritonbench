import torch
import triton
from tritonbench.utils.path_utils import add_path, SUBMODULE_PATH
from tritonbench.utils.triton_op import IS_FBCODE

try:
    from hammer.ops.triton.utils import prev_power_of_2

    # Internal Import
    from hammer.oss.generative_recommenders.ops.triton.triton_ragged_hstu_attention import (
        _ragged_hstu_attn_fwd,
        _ragged_hstu_attn_fwd_persistent,
    )
except ModuleNotFoundError:
    # OSS Import
    with add_path(str(SUBMODULE_PATH.joinpath("generative-recommenders"))):
        from generative_recommenders.ops.triton import triton_ragged_hstu_attention
        _ragged_hstu_attn_fwd_persistent = (
            triton_ragged_hstu_attention._ragged_hstu_attn_fwd_persistent
        )
        _RaggedAttentionRelativeBiasFunction = triton_ragged_hstu_attention._RaggedAttentionRelativeBiasFunction

    @torch.fx.wrap
    def prev_power_of_2(x: int) -> int:
        if torch.compiler.is_compiling():
            # Re-write to make Dynamo happy
            x_tensor = torch.scalar_tensor(x, dtype=torch.int64)  # type: ignore[arg-type]
            x_tensor_orig = x_tensor.clone()
            out = triton.next_power_of_2(x_tensor)  # type: ignore[arg-type]
            return int(torch.where(torch.lt(x_tensor_orig, out), out // 2, out).item())  # type: ignore[return-value]
        else:
            out = triton.next_power_of_2(x)
            return out // 2 if out > x else out


from typing import Tuple


class RaggedHSTUAttn(torch.nn.Module):
    def __init__(
        self,
        args,
        persistent_kernel: bool = False,
    ) -> None:
        super().__init__()
        self.requires_grad = args.requires_grad
        self.batch_size = args.batch_size
        self.num_heads = args.num_heads
        self.max_seq_len = args.max_seq_len
        self.num_buckets = args.num_buckets
        self.alpha = 1.0 / args.attn_dim
        self.invalid_attn_mask_type = "lower_triangular"
        lengths = generate_sparse_seq_len(
            size=batch_size,
            max_seq_len=seq_len,
            sparsity=seq_sparsity,
            device=torch.device("cuda"),
        )
        lengths = apply_SL(lengths, args.sl_alpha, max_seq_len=seq_len)

        self.all_ts_weights = torch.nn.Parameter(
            torch.randn(
                (self.num_buckets + 1,),
                dtype=torch.bfloat16,
            ).requires_grad_(self.requires_grad).cuda()
        )
        self.all_pos_weights = torch.nn.Parameter(
            torch.randn(
                (2 * self.max_seq_len - 1,),
                dtype=torch.bfloat16,
            ).requires_grad_(self.requires_grad).cuda()
        )
        self.persistent_kernel = persistent_kernel


    def forward(
        self, **kwargs
    ) -> torch.Tensor:

        q = qkv[:, :, :128]
        k = qkv[:, :, 128:256]
        v = qkv[:, :, 256:384]
        out = torch.zeros_like(v)

        Z = timestamps.size(0)
        N = timestamps.size(1) - 1
        _, H, DimQ = q.shape
        _, _, DimV = v.shape

        kwargs = {
            "Q": q,
            "K": k,
            "V": v,
            "seq_offsets": seq_offsets,
            "delta_x_offsets": None,
            "TS": timestamps,
            "TW": self.all_ts_weights,
            "PW": self.all_pos_weights,
            "Bias": None,
            "seq2_offsets": None,
            "num_targets": None,
            "Scale": None,
            "Out": out,
            "stride_qm": q.stride(0),
            "stride_qh": q.stride(1),
            "stride_kn": k.stride(0),
            "stride_kh": k.stride(1),
            "stride_vn": v.stride(0),
            "stride_vh": v.stride(1),
            "stride_sz": None,
            "stride_sm": None,
            "stride_ts": timestamps.stride(0),
            "stride_om": out.stride(0),
            "stride_oh": out.stride(1),
            "alpha": 0.08838834764831843,
            "Z": Z,
            "H": H,
            "MAX_SEQ_LEN": N,
            "AUTOTUNE_MAX_SEQ_LEN": prev_power_of_2(N),
            "DimQ": DimQ,
            "DimV": DimV,
            "DeltaSize": None,
            "num_buckets": NUM_BUCKETS,
            "max_pos_ind": None,
            "time_bucket_incr": 60.0,
            "time_bucket_div": 1.0,
            "time_delta": 0.0,
            "INVALID_MASK_TYPE": "lower_triangular",
            "CAUSAL": True,
            "BUCKET_FN": "sqrt",
            "ATTN_BIAS_TYPE": "fused",
            "USE_TIME_BIAS": False,
            "USE_POS_BIAS": False,
            "HAS_MAX_POS_IND": False,
            "HAS_MULTIPLE_TARGETS": False,
            "HAS_ATTN_SCALE": False,
            "IS_DELTA_Q": False,
            "ALLOW_TF32": True,
            "BLOCK_D_Q": DimQ,
            "BLOCK_D_V": DimV,
            "MAX_ATTN_LEN": 0,
            "HAS_CONTEXTUAL_SEQ_LEN": False,
            "contextual_seq_len": 0,
            "HAS_SORT_BY_LENGTH_INDICES": False,
            "sort_by_length_indices": None,
        }

        if self.persistent_kernel:
            grid = (1216,)
            _ragged_hstu_attn_fwd_persistent[grid](**kwargs)
        else:
            return _RaggedAttentionRelativeBiasFunction.apply(
                self.max_seq_len, # N
                kwargs["alpha"],
                q,
                k,
                v,
                kwargs["seq_offsets"],
                kwargs["INVALID_MASK_TYPE"],
                timestamps,
                self.all_ts_weights, # ts_weights
                self.all_pos_weights, # pos_weights
                kwargs["CAUSAL"], # causal,
                kwargs["num_buckets"], # num_buckets
                "sqrt", # time_bucket_fn
                kwargs["time_bucket_incr"], # time_bucket_incr
                kwargs["time_bucket_div"], # time_bucket_div
                kwargs["time_delta"], # time_delta
                kwargs["max_pos_ind"], # max_pos_ind
                kwargs["num_targets"],
                None, # attn_scale
                kwargs["ATTN_BIAS_TYPE"], # relative_bias_type
                kwargs["MAX_ATTN_LEN"], # max_attn_len
                kwargs["contextual_seq_len"], # contextual_seq_len
                kwargs["sort_by_length_indices"] # sort_by_length
            )

        return out


def get_test_inputs(
    args
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    causal = True
    time_delta = 0.0
    num_buckets = 2048
    time_bucket_fn = "sqrt"
    time_bucket_incr = 60
    time_bucket_div = 1.0

    timestamps = generate_hstu_timestamps(batch_size, seq_len)
    num_buckets = args.num_buckets
    seq_len = args.seq_len
    seq2_offsets = torch.zeros(
            (batch_size + 1,),
            dtype=torch.int64,
            device=torch.device("cuda"),
        )
    seq2_offsets[1:] = torch.cumsum(lengths * lengths, dim=0)

    ts_weights: torch.Tensor = torch.empty(
            (num_buckets + 1,),
            device="cuda",
            dtype=torch.float32,
        ).uniform_(-0.1, 0.1)
    pos_weights: torch.Tensor = torch.empty(
            (2 * seq_len - 1,),
            device="cuda",
            dtype=torch.float32,
        ).uniform_(-0.1, 0.1)
    if args.requires_grad:
        q = q.requires_grad_(True)
        k = k.requires_grad_(True)
        v = v.requires_grad_(True)
        ts_weights = ts_weights.requires_grad_(True)
        pos_weights = pos_weights.requires_grad_(True)
    return {
        "N": args.max_seq_len,
        "alpha": args.alpha,
        "q": q,
        "k": k,
        "v": v,
        "seq_offsets": ,
        "invalid_attn_mask_type": ,
        "timestamps": ,
        "ts_weights": ,
        "pos_weights": ,
        "causal": causal,
        "num_buciets": ,
        "time_bucket_fn": ,
        "time_bucket_incr": ,
        "time_bucket_div": ,
        "time_delta": ,
        "max_pos_ind": ,
        "num_targets": ,
        "attn_scale": ,
        "relative_bias_type": ,
        "max_attn_len": ,
        "contextual_seq_len": ,
        "sort_by_length": ,
    }
