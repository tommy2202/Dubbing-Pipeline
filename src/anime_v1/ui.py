import pathlib

import gradio as gr
from config.settings import get_settings

from anime_v1.cli import cli as _cli


def _run(video, src, tgt, mode, lipsync, keep_bg):
    # Call CLI function programmatically
    args = [video]
    if src:
        args += ["--src-lang", src]
    if tgt:
        args += ["--tgt-lang", tgt]
    if mode:
        args += ["--mode", mode]
    if lipsync:
        args += ["--lipsync"]
    else:
        args += ["--no-lipsync"]
    if keep_bg:
        args += ["--keep-bg"]
    else:
        args += ["--no-keep-bg"]
    _cli.main(args=args, standalone_mode=False)
    stem = pathlib.Path(video).stem if pathlib.Path(video).exists() else "remote"
    out = pathlib.Path(str(get_settings().v1_output_dir)) / f"{stem}_dubbed.mkv"
    return str(out)


def make_app():
    with gr.Blocks() as demo:
        gr.Markdown("# Offline Dubber")
        with gr.Row():
            inp = gr.Textbox(label="Video path or URL")
            src = gr.Textbox(label="Source lang (e.g. ja)")
            tgt = gr.Textbox(label="Target lang (e.g. en)", value="en")
        with gr.Row():
            mode = gr.Dropdown(["high", "medium", "low"], value="high", label="Mode")
            lipsync = gr.Checkbox(value=True, label="Lip-sync")
            keep_bg = gr.Checkbox(value=True, label="Keep background")
        btn = gr.Button("Dub")
        out = gr.Textbox(label="Output path")
        btn.click(_run, [inp, src, tgt, mode, lipsync, keep_bg], out)
    return demo


if __name__ == "__main__":
    app = make_app()
    s = get_settings()
    app.launch(server_name=str(s.v1_ui_host), server_port=int(s.v1_ui_port))
