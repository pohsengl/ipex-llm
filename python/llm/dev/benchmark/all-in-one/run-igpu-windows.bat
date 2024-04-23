:: accept argument on kv-cache, default 1, user can set to 0
set IPEX_LLM_QUANTIZE_KV_CACHE=%1
if "%IPEX_LLM_QUANTIZE_KV_CACHE%"=="" set IPEX_LLM_QUANTIZE_KV_CACHE=1

set SYCL_CACHE_PERSISTENT=1
set BIGDL_LLM_XMX_DISABLED=1

python run.py