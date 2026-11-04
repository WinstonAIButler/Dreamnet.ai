#!/usr/bin/env bash
# Dreamnet - set up a RunPod GPU pod for pictures (Z-Image-Turbo) and videos (Wan 2.2 TI2V 5B).
#
# Run once in the pod's web terminal (it is safe to run again):
#   COMFY_USER=dreamnet COMFY_PASSWORD='a long password' bash runpod-setup.sh
# Then set the pod's start command to:  bash /workspace/dreamnet-start.sh
#
# Everything lives on /workspace (the pod's volume), so models download once and survive stops.
# ComfyUI listens on 127.0.0.1:8189 only; Caddy puts a password in front of it on port 8188, which is
# the port RunPod's proxy exposes. Without the password nobody can use the GPU or see your pictures.
set -euo pipefail

: "${COMFY_USER:?Set COMFY_USER}"
: "${COMFY_PASSWORD:?Set COMFY_PASSWORD}"
W=/workspace
cd "$W"

echo "== ComfyUI"
if [ ! -d ComfyUI ]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git
fi
cd ComfyUI && git pull --ff-only || true
python3 -m venv "$W/venv" 2>/dev/null || true
"$W/venv/bin/pip" install -q --upgrade pip
"$W/venv/bin/pip" install -q -r requirements.txt "huggingface_hub[cli]"
cd "$W"

echo "== Models (about 30 GB the first time)"
M=$W/ComfyUI/models
get() {  # repo, file in repo, target folder
  local dest="$M/$3/$(basename "$2")"
  if [ -s "$dest" ]; then echo "have $(basename "$2")"; return; fi
  "$W/venv/bin/huggingface-cli" download "$1" "$2" --local-dir "$W/hf"
  mkdir -p "$M/$3" && mv "$W/hf/$2" "$dest"
}
# Pictures: Z-Image-Turbo
get Comfy-Org/z_image_turbo split_files/diffusion_models/z_image_turbo_bf16.safetensors diffusion_models
get Comfy-Org/z_image_turbo split_files/text_encoders/qwen_3_4b.safetensors text_encoders
get Comfy-Org/z_image_turbo split_files/vae/ae.safetensors vae
# Videos: Wan 2.2 TI2V 5B
get Comfy-Org/Wan_2.2_ComfyUI_Repackaged split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors diffusion_models
get Comfy-Org/Wan_2.2_ComfyUI_Repackaged split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors text_encoders
get Comfy-Org/Wan_2.2_ComfyUI_Repackaged split_files/vae/wan2.2_vae.safetensors vae
rm -rf "$W/hf"

echo "== Caddy (password in front of ComfyUI)"
if [ ! -x "$W/caddy" ]; then
  curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=amd64" -o "$W/caddy"
  chmod +x "$W/caddy"
fi
HASH=$("$W/caddy" hash-password --plaintext "$COMFY_PASSWORD")
cat > "$W/Caddyfile" <<EOF
:8188 {
  basic_auth {
    $COMFY_USER $HASH
  }
  reverse_proxy 127.0.0.1:8189
}
EOF

cat > "$W/dreamnet-start.sh" <<'EOF'
#!/usr/bin/env bash
# Start ComfyUI (local only) and Caddy (password, public port 8188).
cd /workspace/ComfyUI
nohup /workspace/venv/bin/python main.py --listen 127.0.0.1 --port 8189 > /workspace/comfyui.log 2>&1 &
nohup /workspace/caddy run --config /workspace/Caddyfile --adapter caddyfile > /workspace/caddy.log 2>&1 &
sleep infinity
EOF
chmod +x "$W/dreamnet-start.sh"

echo
echo "Done. Set the pod's start command to:  bash /workspace/dreamnet-start.sh"
echo "In Dreamnet set COMFYUI_USER=$COMFY_USER and COMFYUI_PASSWORD to the same password."
