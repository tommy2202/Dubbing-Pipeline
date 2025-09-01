Anime Dub Alpha:

This version is very basic, however it is completely working and running how it is expected to. The abilities of this version is:
Transcribes a Japanese video into English

Creates accurate subtitles

Can be used with either CLI or GUI


------------------------------------------------------------------------------------------------------------------------------------------


Version One Changelog:


| Area                   | Alpha (demo Flask)                                    | Version 1 (this build)                                                 |
| ---------------------- | ------------------------------------------------------|----------------------------------------------------


| **Build**              | Unpinned `requirements.txt`; non-deterministic Docker | **Deterministic build** – `docker/constraints.txt`, hash-pinned wheels |


| **Models**             | Whisper‐small JP→JP, Coqui VCTK TTS, no diarisation   | Whisper‐small **translate**, pyannote diarisation stub, real Coqui TTS |


| **Pipeline structure** | Monolithic `pipeline.py`                              | Modular `src/anime_v1/stages/*` with JSON checkpoints                  


|
| **CLI**                | Bash wrapper only                                     | `anime-v1` Click CLI: `run` sub-command, checkpoints, profiles         |


| **GUI**                | Minimal Flask demo                                    | PyQt single-window                       
|


| **Output folders**     | `Input/`, `Output/` hard-coded                        | Same layout, now produced by `mkv_export.py` (soft-sub MKV)            |


| **Logging**            | Print statements                                      | Central logger + Prometheus metrics endpoint                           |


| **Tests / CI**         | none                                                  | Pytest scaffold & GitHub Action                         |


| **Licensing**          | MIT code, but builds weren’t repeatable               | MIT/Apache/CC-BY only, deterministic and audit-ready                   |


Commands:


Task: Shows where the folder is

Command: cd (The directory where the root folder is)


Task: Build Image

Command: docker build -f docker\Dockerfile -t (What version your using) .


Task: Run Dubbing CLI
Command: 

docker run --rm ^
   -v "%cd%\Input":/data/in ^
   -v "%cd%\Output":/data/out ^
   -v "%cd%":/app ^
   anime-v1 /data/in/Talking.mp4


Task: Run Web GUI (Alpha only)

Command:

docker run --rm -p 5000:5000 ^
  -v "%cd%:/app" ^
  -v "%cd%\Input:/app/Input" ^
  -v "%cd%\Output:/app/Output" ^
  anime-dubbing-alpha python -m anime_dubbing_alpha.webgui



Task: Run Web GUI (Version Two onwards)

Command: 

docker run --rm -p 5000:5000 ^
  -v "%cd%":/app ^
  anime-v1 gui



Task: 

Command: 