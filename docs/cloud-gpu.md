# Cloud GPU for pictures and videos

Dreamnet can make pictures and videos on a GPU you rent from [RunPod](https://www.runpod.io) by the hour.
You switch it on from the **Create image** page, and Dreamnet switches it off again after
`GPU_IDLE_MINUTES` (default 15) without use, so you only pay while it works.

On your own GPU the **Picture heat** setting applies (Tasteful, Sensual or Explicit). Online services
(the free image service and Replicate) always get Tasteful. Whatever the setting, every person shown is
an adult and never a real, identifiable person, and prompts that describe a minor are refused.

## 1. Make the pod (once)

1. Create a RunPod account and add some credit (about $10 is plenty to start).
2. **Storage > Network Volume**: create a 60 GB volume in a region that has RTX 4090s. Models live
   here so they download only once.
3. **Pods > Deploy**: pick an **RTX 4090** (24 GB) on that volume, template **RunPod PyTorch**
   (any recent one), and expose **HTTP port 8188**.
4. When it's running, open its **Web Terminal**, then:

   ```bash
   cd /workspace
   curl -fsSLO https://raw.githubusercontent.com/WinstonAIButler/Dreamnet.ai/main/scripts/runpod-setup.sh
   COMFY_USER=dreamnet COMFY_PASSWORD='pick a long password' bash runpod-setup.sh
   ```

   The first run downloads about 30 GB of models (10–20 minutes).
5. **Edit Pod > Container Start Command**: `bash /workspace/dreamnet-start.sh`, then restart the pod.
6. Check `https://<pod id>-8188.proxy.runpod.net` in a browser: it should ask for that login, then show
   ComfyUI.

## 2. Connect Dreamnet

In Render (**Environment**) or your `.env`:

| Setting | Value |
|---|---|
| `RUNPOD_API_KEY` | RunPod **Settings > API Keys** (a key with access to pods) |
| `RUNPOD_POD_ID` | The pod's id, as shown on its card |
| `COMFYUI_USER` / `COMFYUI_PASSWORD` | The login from step 4 |
| `GPU_IDLE_MINUTES` | Optional; minutes without use before the pod stops (default 15, 0 = never) |

Leave `COMFYUI_URL` empty: with a pod id, Dreamnet uses the pod's own address. Redeploy, open
**Create image**, press **Start GPU**, and wait for "On and ready" (about 1–2 minutes).

## Costs and speed (estimates)

An RTX 4090 pod is roughly $0.35–$0.70 an hour, plus a few dollars a month for the volume. A picture
takes seconds; a 5-second Wan 2.2 clip takes a few minutes. Check RunPod's current prices.

## Video settings

| Setting | Default | |
|---|---|---|
| `VIDEO_BACKEND` | `auto` | `comfyui`, `replicate`, or `auto` (the cloud GPU when set up) |
| `VIDEO_FRAMES` | `121` | 121 frames at 24 fps is about 5 seconds |
| `VIDEO_STEPS` | `20` | Fewer is faster, more is cleaner |
| `WAN_UNET`, `WAN_CLIP`, `WAN_VAE` | the setup script's files | Only change these if you use other files |
