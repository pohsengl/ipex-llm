#
# Copyright 2016 The BigDL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Some parts of this file is adapted from
# https://github.com/huggingface/transformers/blob/v4.40.0/src/transformers/models/llama/modeling_llama.py
# which is licensed under Apache License 2.0:
#
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from typing import Optional, Tuple, List

import math
import torch
from transformers.cache_utils import Cache
from transformers.models.stablelm.modeling_stablelm import repeat_kv, apply_rotary_pos_emb
from transformers.models.stablelm.modeling_stablelm import StableLmAttention, StableLmMLP, \
    StableLmModel

from ipex_llm.transformers.npu_models.common import merge_linear


def merge_qkv(module: torch.nn.Module):
    if isinstance(module, StableLmAttention):
        new_weight = torch.cat([
            module.q_proj.weight.data,
            module.k_proj.weight.data,
            module.v_proj.weight.data,
        ], dim=0)

        if module.q_proj.bias is not None:
            qkv_proj = torch.nn.Linear(0, 0, bias=True)
            new_bias = torch.cat([
                module.q_proj.bias.data,
                module.k_proj.bias.data,
                module.v_proj.bias.data,
            ], dim=0)
            qkv_proj.bias = torch.nn.Parameter(new_bias, requires_grad=False)
        else:
            qkv_proj = torch.nn.Linear(0, 0, bias=False)
        qkv_proj.weight = torch.nn.Parameter(new_weight, requires_grad=False)
        qkv_proj.in_features = new_weight.size(1)
        qkv_proj.out_features = new_weight.size(0)
        module.qkv_proj = qkv_proj

        del module.q_proj, module.k_proj, module.v_proj


def merge_mlp(module: torch.nn.Module):
    if isinstance(module, StableLmMLP):
        gate_up_proj = merge_linear([
            module.gate_proj,
            module.up_proj,
        ])
        module.gate_up_proj = gate_up_proj
        del module.gate_proj, module.up_proj


def stablelm_model_forward(
    self,
    input_ids: torch.LongTensor = None,
    attention_mask: Optional[torch.Tensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    past_key_values: Optional[List[torch.FloatTensor]] = None,
    inputs_embeds: Optional[torch.FloatTensor] = None,
    use_cache: Optional[bool] = None,
    output_attentions: Optional[bool] = None,
    output_hidden_states: Optional[bool] = None,
    return_dict: Optional[bool] = None,
):
    # ipex-llm changes start
    from ipex_llm.transformers.kv import DynamicNormalCache
    # IPEX-LLM OPT: kv cache and quantize kv cache
    use_cache = use_cache if use_cache is not None else self.config.use_cache
    if use_cache:
        if not isinstance(past_key_values, DynamicNormalCache):
            past_key_values = DynamicNormalCache.from_legacy_cache(past_key_values)
    return StableLmModel.forward(
        self=self,
        input_ids=input_ids,
        attention_mask=attention_mask,
        position_ids=position_ids,
        past_key_values=past_key_values,
        inputs_embeds=inputs_embeds,
        use_cache=use_cache,
        output_attentions=output_attentions,
        output_hidden_states=output_hidden_states,
        return_dict=return_dict,
    )


def stablelm_attention_forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: Optional[torch.Tensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    past_key_value: Optional[Cache] = None,
    output_attentions: bool = False,
    use_cache: bool = False,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
    bsz, q_len, _ = hidden_states.size()

    qkv = self.qkv_proj(hidden_states)
    qkv = qkv.view(bsz, q_len, self.num_heads + 2 * self.num_key_value_heads, self.head_dim)
    qkv = qkv.transpose(1, 2)
    query_states, key_states, value_states = qkv.split([self.num_heads,
                                                        self.num_key_value_heads,
                                                        self.num_key_value_heads], dim=1)
    # For stablelm-2-12b's qk per-head norm
    if getattr(self, "qk_layernorm", False):
        query_states = self.q_layernorm(query_states)
        key_states = self.k_layernorm(key_states)

    kv_seq_len = key_states.shape[-2]
    if past_key_value is not None:
        kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)

    # Partial rotary embedding
    # [batch_size, num_heads, seq_length, head_dim * config.partial_rotary_factor]
    rot_dim = self.rotary_emb.dim

    query_rot, query_pass = query_states[..., :rot_dim], query_states[..., rot_dim:]
    key_rot, key_pass = key_states[..., :rot_dim], key_states[..., rot_dim:]
    cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
    query_rot, key_rot = apply_rotary_pos_emb(query_rot,
                                              key_rot,
                                              cos,
                                              sin,
                                              position_ids)
    query_states = torch.cat((query_rot, query_pass), dim=-1)
    key_states = torch.cat((key_rot, key_pass), dim=-1)

    if past_key_value is not None:
        # Specific to RoPE models with partial rotation
        cache_kwargs = {"sin": sin, "cos": cos, "partial_rotation_size": self.rotary_emb.dim}
        key_states, value_states = past_key_value.update(key_states, value_states,
                                                         self.layer_idx, cache_kwargs)

    # repeat k/v heads if n_kv_heads < n_heads
    key_states = repeat_kv(key_states, self.num_key_value_groups)
    value_states = repeat_kv(value_states, self.num_key_value_groups)

    attn_weights = torch.matmul(query_states,
                                key_states.transpose(2, 3)) / math.sqrt(self.head_dim)

    if attention_mask is not None:
        attn_weights = attn_weights + attention_mask

    # upcast attention to fp32
    attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1,
                                               dtype=torch.float32).to(value_states.dtype)
    attn_weights = self.attention_dropout(attn_weights)
    attn_output = torch.matmul(attn_weights, value_states)

    attn_output = attn_output.transpose(1, 2).contiguous()
    attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)
    attn_output = self.o_proj(attn_output)

    if not output_attentions:
        attn_weights = None

    return attn_output, attn_weights, past_key_value


def stablelm_mlp_forward(self, x):
    gate_up_proj = self.gate_up_proj(x)
    gate_proj, up_proj = gate_up_proj.chunk(2, dim=-1)
    down_proj = self.down_proj(self.act_fn(gate_proj) * up_proj)
    return down_proj
