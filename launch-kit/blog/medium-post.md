# Free Topaz Labs Alternative: Upscale AI-Generated Videos to 4K and Photos to 8K

**NextGenUp is a free, open source AI upscaler for images and video. It runs entirely on your computer, restores faces, and costs exactly nothing. Here is how it compares to Topaz, Upscayl, and Video2X.**

*Tip for Medium: paste this text into the editor, then insert the images from the URLs where marked. Set the cover image to the first URL. Medium has no tables, so the comparison is included both as an image and as text.*

Cover image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/social-preview.png

---

If you generate videos with Sora, Runway, Grok, or any other AI video tool, you know the problem: the output is usually 720p, sometimes 480p. It looks fine on a phone and soft everywhere else.

The standard answer is Topaz Video AI and Gigapixel, which are excellent and cost about $199 per year. The free answers are mostly single-purpose: Upscayl handles images only, Video2X handles video but expects command-line comfort and manual model downloads.

I wanted one simple app that upscales images, video, and faces, for free, without sending my files to anyone's cloud. It did not exist, so I built it.

## Meet NextGenUp

NextGenUp is a free, open source AI upscaler with a point-and-click interface. Drop in a file, pick a mode, press Start. A 720p AI-generated video becomes 4K. A small photo becomes 8K. Old family photos get their faces restored.

Image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/compare.png

Under the hood it uses Real-ESRGAN, the same family of super-resolution models behind many commercial tools, plus GFPGAN for face restoration. Everything runs locally on your machine. Nothing is uploaded anywhere.

## NextGenUp vs Topaz vs Upscayl vs Video2X

Image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/launch-kit/blog/comparison-chart.png

The same comparison in plain text:

**Price.** NextGenUp: free, MIT licensed. Topaz Photo AI and Video AI: about $199 per year. Upscayl: free. Video2X: free.

**Image upscaling to 8K.** NextGenUp: yes. Topaz: yes. Upscayl: yes. Video2X: no.

**Video upscaling to 4K.** NextGenUp: yes. Topaz: yes. Upscayl: no. Video2X: yes.

**Face restoration.** NextGenUp: yes, with GFPGAN. Topaz: yes. Upscayl: no. Video2X: no.

**Same-size enhance (clean up an image without resizing).** NextGenUp: yes. Topaz: yes. Upscayl: no. Video2X: no.

**Batch processing with zip export.** NextGenUp: yes. Topaz: yes. Upscayl: yes. Video2X: partial.

**Fully offline and private.** All four: yes.

**Point-and-click interface.** NextGenUp, Topaz, and Upscayl: yes. Video2X: partial, it is more technical to set up.

**Usable from any device on your network.** Only NextGenUp: it can serve its interface to any browser on your LAN, including phones and tablets.

The honest summary: on extreme fine detail at 4x, Topaz still has an edge. For AI-generated media, web use, social media, prints, and restored family photos, the results are comparable, and the price difference is $199 per year versus zero. Among the free tools, NextGenUp is the only one that covers images, video, and faces in one app.

## What using it looks like

Image: https://raw.githubusercontent.com/riponcm/nextgenUp/main/assets/screens/modes.png

Four image modes cover the range from instant to maximum quality, including Enhance, my personal favorite: it reconstructs detail with AI at 4x, then scales back to the original size. Same dimensions, but the blur, noise, and JPEG artifacts are gone. For video there is a fast FFmpeg mode and a frame-by-frame AI mode, with audio preserved and a cancel button when you change your mind.

Every result gets a before/after slider, so you judge the quality yourself instead of trusting a marketing page.

## Free means free

No trial, no subscription, no watermark, no account, no telemetry. Desktop apps for macOS (Apple Silicon and Intel), Windows, and Linux, with all AI models and FFmpeg bundled. Install, open, upscale.

## Try it

Download from the releases page, or run it from source:

**github.com/riponcm/nextgenUp**

If you find it useful, starring the repository helps other people discover a free alternative. Feedback and feature requests are welcome.

NextGenUp is a matily.org product.
