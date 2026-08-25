# I Was Tired of Paying for Image Upscalers, So I Built a Free One

**NextGenUp upscales photos to 8K and videos to 4K with AI, runs entirely on your computer, and costs nothing. Here is what it does and why I made it.**

*Tip for Medium: paste this text into the editor, then insert the images from the URLs where marked. Set the cover image to the banner (first URL).*

Cover image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/social-preview.png

---

Every few months I end up with the same problem. A batch of AI-generated videos at 720p. Old family photos that look soft on a modern screen. A logo I only have in a tiny size.

The tools that fix this well are excellent, but they cost around $199 per year. The free alternatives are mostly single-purpose: one does images only, another does video only and expects you to be comfortable with a command line and manual model downloads.

I wanted one simple app that does images, video, and faces, for free. It did not exist, so I built it.

## Meet NextGenUp

NextGenUp is a free, open source AI upscaler for images and video. You drop in a file, pick a mode, and press Start. That is the whole workflow.

Image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/compare.png

Under the hood it uses Real-ESRGAN, the same family of super-resolution models behind many commercial tools, plus GFPGAN for face restoration. Everything runs locally on your machine. Your photos and videos are never uploaded anywhere, which matters more than people think when the photos are of your family.

## What it can do

**Upscale images up to 8K.** Four modes cover the range from instant to maximum quality, including an Ultra mode that runs on a local server so even a phone on your network can use it.

**Upscale video to 4K.** A 720p clip becomes 3840 x 2160, audio preserved, portrait and landscape handled automatically.

**Make images clearer without resizing.** The Enhance mode is my favorite: it reconstructs detail with AI at 4x, then scales back to the original size. Same dimensions, but the blur, noise, and JPEG artifacts are gone.

**Restore faces.** Old, scanned, or compressed photos of people benefit the most. Every detected face is restored and blended back invisibly.

Image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/modes.png

**Work in batches.** Drop twenty photos, process them all, download a zip.

**Compare honestly.** Every result gets a before/after slider, so you can judge the quality yourself instead of trusting a marketing page.

## Free means free

NextGenUp is MIT licensed. There is no trial, no subscription, no watermark, no account, and no telemetry. Desktop apps exist for macOS (Apple Silicon and Intel), Windows, and Linux, with all the AI models and FFmpeg bundled, so there is nothing to configure.

Is it better than the paid tools? On extreme fine detail at 4x, the commercial options still have an edge. For web use, social media, prints, restored family photos, and AI-generated media, the results are comparable, and the price difference is $199 per year versus zero.

## Try it

Download it from the releases page on GitHub, or run it from source if you prefer:

**github.com/riponcm/nextgenUp**

If you find it useful, starring the repository is the best way to help other people discover a free alternative. Feedback and feature requests are welcome in the issues.

NextGenUp is a matily.org product.
