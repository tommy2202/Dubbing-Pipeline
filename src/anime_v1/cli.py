import click, pathlib
from anime_v1.stages import audio_extractor, diarisation, transcription, tts, mkv_export
from anime_v1.utils import logger

@click.command()
@click.argument('video', type=click.Path(exists=True))
def cli(video):
    video = pathlib.Path(video)
    ckpt = pathlib.Path('checkpoints')/video.stem
    ckpt.mkdir(parents=True, exist_ok=True)
    wav = audio_extractor.run(video, ckpt_dir=ckpt)
    diarisation.run(wav, ckpt_dir=ckpt)
    transcript_json = transcription.run(wav, ckpt_dir=ckpt)
    tts.run(transcript_json, ckpt_dir=ckpt)
    mkv_export.run(video, ckpt_dir=ckpt)

if __name__ == '__main__':
    cli()
