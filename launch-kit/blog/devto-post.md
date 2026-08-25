---
title: I Built a Free, Open Source Alternative to Topaz for Image and Video Upscaling
published: false
description: NextGenUp upscales photos to 8K and videos to 4K locally with Real-ESRGAN and GFPGAN. Here is what it does and what I learned building it.
tags: opensource, ai, showdev, python
cover_image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/social-preview.png
---

Most of my AI-generated videos come out at 720p. The tools that upscale them well are either subscription software at around $199 per year, or open source models that need a command line, separate weight downloads, and a lot of patience.

So I built [NextGenUp](https://github.com/riponcm/nextgenUp): a free, open source AI upscaler for both images and video, with a point-and-click interface. Everything runs locally. Nothing you upscale ever leaves your machine.

![Before and after comparison slider](https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/compare.png)

## What it does

- **Images up to 8K** with four modes: Quick (browser AI), Quality (FFmpeg Lanczos plus CAS sharpening), Enhance (same resolution, AI cleanup), and Ultra (server-side AI that works from any device)
- **Video up to 4K**, with audio preserved and portrait or landscape handled automatically
- **Face restoration** with GFPGAN: detected faces are aligned, restored, and blended back seamlessly
- **Batch processing** with a zip download, a before/after slider on every result, and cancellable jobs
- **Desktop apps** for macOS, Windows, and Linux, with FFmpeg and the models bundled, so there is no setup

It is MIT licensed. There is no account, no watermark, and no telemetry.

## How it works

The architecture is a local Flask server plus a web UI, wrapped in Tauri for the desktop:

- **Super-resolution** uses Real-ESRGAN (the compact realesr-general-x4v3 model, about 5 MB as ONNX). In the browser it runs through ONNX Runtime Web on WebGPU, with a WASM fallback. On the server it runs through ONNX Runtime on CPU.
- **Tiling** keeps memory bounded: images are processed in overlapping tiles (64 to 128 px with padding), then stitched. This is why a 6000 px photo works on modest hardware.
- **Face restoration** chains YuNet detection, a similarity transform to the FFHQ template, GFPGAN v1.4 inference at 512x512, and a feathered paste-back.
- **The desktop app** is a thin Rust shell that spawns the Python server as a PyInstaller sidecar and points a webview at it. Auto-updates are handled by the Tauri updater with signed releases on GitHub.

![Image modes](https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/modes.png)

## Three bugs worth sharing

**The 95 percent hang.** Video encoding kept freezing near the end. Cause: I piped both stdout and stderr from FFmpeg but only read stdout. FFmpeg filled the 64 KB stderr pipe buffer, blocked on writing, and we deadlocked. Fix: drain stderr in a background thread. If you spawn FFmpeg from any language, you have probably hit this.

**The orphaned server.** Quitting the desktop app left the backend running. PyInstaller one-file binaries run as a bootloader that spawns the real process as a child, so killing the direct child orphans the grandchild. Fix: put the sidecar in its own process group on Unix and kill the group; use taskkill /T on Windows.

**The relocated FFmpeg.** I bundled the ffmpeg binary from my own machine and every server feature silently broke in the packaged app. It was a dynamically linked build that only works from its original install directory. Always bundle static builds.

## Honest limitations

The compact Real-ESRGAN model does not beat Topaz on extreme fine detail like hair at 4x. Pro video mode is slow without WebGPU. The macOS builds are not notarized yet, so the first launch needs right-click and Open. A max-quality tier with larger models (Real-ESRGAN x4plus or HAT) is on the roadmap.

## Try it

Installers for macOS, Windows, and Linux are on the [releases page](https://github.com/riponcm/nextgenUp/releases/latest), or run it from source with Python and FFmpeg. If it is useful to you, a star on [GitHub](https://github.com/riponcm/nextgenUp) helps other people find it.

I would love feedback, especially from anyone who has compared upscaling models in practice.

NextGenUp is a [matily.org](https://matily.org) product, MIT licensed.
