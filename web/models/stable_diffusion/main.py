import torch
from PIL import Image
from tqdm.auto import tqdm
from models.stable_diffusion.cache_objects import (
    cache_obj,
    schedulers,
)
from models.stable_diffusion.stable_args import args
from random import randint
import numpy as np
import time


def set_ui_params(prompt, negative_prompt, steps, guidance_scale, seed):
    args.prompts = [prompt]
    args.negative_prompts = [negative_prompt]
    args.steps = steps
    args.guidance_scale = guidance_scale
    args.seed = seed


def stable_diff_inf(
    prompt: str,
    negative_prompt: str,
    steps: int,
    guidance_scale: float,
    seed: int,
    scheduler_key: str,
):

    # Handle out of range seeds.
    uint32_info = np.iinfo(np.uint32)
    uint32_min, uint32_max = uint32_info.min, uint32_info.max
    if seed < uint32_min or seed >= uint32_max:
        seed = randint(uint32_min, uint32_max)

    guidance_scale = torch.tensor(guidance_scale).to(torch.float32)
    set_ui_params(prompt, negative_prompt, steps, guidance_scale, seed)
    dtype = torch.float32 if args.precision == "fp32" else torch.half
    generator = torch.manual_seed(
        args.seed
    )  # Seed generator to create the inital latent noise

    # set height and width.
    height = 512  # default height of Stable Diffusion
    width = 512  # default width of Stable Diffusion
    if args.version == "v2.1":
        height = 768
        width = 768

    # Initialize vae and unet models.
    vae, unet, clip, tokenizer = (
        cache_obj["vae"],
        cache_obj["unet"],
        cache_obj["clip"],
        cache_obj["tokenizer"],
    )
    scheduler = schedulers[scheduler_key]
    cpu_scheduling = not scheduler_key.startswith("Shark")

    start = time.time()
    text_input = tokenizer(
        args.prompts,
        padding="max_length",
        max_length=args.max_length,
        truncation=True,
        return_tensors="pt",
    )

    clip_inf_start = time.time()
    text_embeddings = clip.forward((text_input.input_ids,))
    clip_inf_end = time.time()
    text_embeddings = torch.from_numpy(text_embeddings).to(dtype)
    max_length = text_input.input_ids.shape[-1]

    uncond_input = tokenizer(
        args.negative_prompts,
        padding="max_length",
        max_length=max_length,
        truncation=True,
        return_tensors="pt",
    )
    uncond_clip_inf_start = time.time()
    uncond_embeddings = clip.forward((uncond_input.input_ids,))
    uncond_clip_inf_end = time.time()
    uncond_embeddings = torch.from_numpy(uncond_embeddings).to(dtype)

    text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

    latents = torch.randn(
        (1, 4, height // 8, width // 8),
        generator=generator,
        dtype=torch.float32,
    ).to(dtype)

    scheduler.set_timesteps(args.steps)
    scheduler.is_scale_input_called = True

    latents = latents * scheduler.init_noise_sigma
    text_embeddings_numpy = text_embeddings.detach().numpy()

    avg_ms = 0
    out_img = None
    for i, t in tqdm(enumerate(scheduler.timesteps)):

        step_start = time.time()
        timestep = torch.tensor([t]).to(dtype).detach().numpy()
        latent_model_input = scheduler.scale_model_input(latents, t)
        if cpu_scheduling:
            latent_model_input = latent_model_input.detach().numpy()

        noise_pred = unet.forward(
            (
                latent_model_input,
                timestep,
                text_embeddings_numpy,
                args.guidance_scale,
            ),
            send_to_host=False,
        )

        if cpu_scheduling:
            noise_pred = torch.from_numpy(noise_pred.to_host())
            latents = scheduler.step(noise_pred, t, latents).prev_sample
        else:
            latents = scheduler.step(noise_pred, t, latents)
        step_time = time.time() - step_start
        avg_ms += step_time
        step_ms = int((step_time) * 1000)
        print(f" \nIteration = {i}, Time = {step_ms}ms")

    # scale and decode the image latents with vae
    latents = 1 / 0.18215 * latents
    latents_numpy = latents
    if cpu_scheduling:
        latents_numpy = latents.detach().numpy()
    vae_start = time.time()
    image = vae.forward((latents_numpy,))
    vae_end = time.time()
    image = torch.from_numpy(image)
    image = (image.detach().cpu().permute(0, 2, 3, 1) * 255.0).numpy()
    images = image.round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]
    out_img = pil_images[0]

    avg_ms = 1000 * avg_ms / args.steps
    total_time = time.time() - start

    text_output = f"prompt={args.prompts}"
    text_output += f"\nnegative prompt={args.negative_prompts}"
    text_output += f"\nsteps={args.steps}, guidance_scale={args.guidance_scale}, scheduler={scheduler_key}, seed={args.seed}, size={height}x{width}, version={args.version}"
    text_output += "\nAverage step time: {0:.2f}ms/it".format(avg_ms)
    print(f"\nAverage step time: {avg_ms}ms/it")
    text_output += "\nTotal image generation time: {0:.2f}sec".format(
        total_time
    )
    clip_inf_time = (clip_inf_end - clip_inf_start) * 1000
    uncond_clip_inf_time = (uncond_clip_inf_end - uncond_clip_inf_start) * 1000
    avg_clip_inf = (clip_inf_time + uncond_clip_inf_time) / 2
    vae_inf_time = (vae_end - vae_start) * 1000
    print(
        f"Clip Inference Avg time (ms) = ({clip_inf_time:.3f} + {uncond_clip_inf_time:.3f}) / 2 = {avg_clip_inf:.3f}"
    )
    print(f"VAE Inference time (ms): {vae_inf_time:.3f}")
    print(f"\nTotal image generation time: {total_time}sec")

    return out_img, text_output
