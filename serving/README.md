# serving/ — the GPU VM tier

Two things live here:

| File | Runs where | Purpose |
|---|---|---|
| `docker-compose.vllm.yml` | **GPU serving VM** | Starts one vLLM process per model |
| `models.dev.yaml` / `models.prod.yaml` | **Gateway VM** (mounted into the broker) | Maps public model name → upstream URL |

They live in the same directory on purpose: adding a model means editing
*both* files, and you want that to be one atomic commit.

## Why the GPU tier has no repo of its own

There is no application code here — vLLM is an off-the-shelf image and this is
its launch configuration. Splitting it out would create a repo containing one
compose file, and every model addition would need two coordinated PRs across
two repos to stay consistent. See the "Repository layout" section of the root
README.

## Bringing up the GPU VM

```bash
scp -r serving/ .env gpu-vm:~/
ssh gpu-vm
cd serving
docker compose -f docker-compose.vllm.yml up -d
docker compose -f docker-compose.vllm.yml logs -f      # first start downloads weights
```

Verify from the gateway VM (not from your laptop — this port is private):

```bash
curl -H "Authorization: Bearer $VLLM_API_KEY" http://<gpu-vm-private-ip>:8000/v1/models
```

Then point the broker at it by mounting `models.prod.yaml` instead of
`models.dev.yaml` in the root `docker-compose.yml`.

## Model choice

The PoC uses **`Qwen/Qwen2.5-7B-Instruct`** — open weights (Apache 2.0), no
gating, strong multilingual instruct performance, and well-supported by vLLM.

- Small GPU (<16GB VRAM): use `Qwen/Qwen2.5-1.5B-Instruct`.
- 2× GPUs: add `--tensor-parallel-size=2`.
- Newer Qwen generations work the same way — change `--model` and the
  `upstream_model` in `models.prod.yaml`, nothing else.

Weights download from Hugging Face on first start. Open models need no token;
set `HF_TOKEN` only if you later use a gated repo.
