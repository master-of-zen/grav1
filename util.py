import subprocess, re, contextlib, os
from tempfile import NamedTemporaryFile

re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}).(\d{2})", re.U)
re_position = re.compile(r".*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", re.U)

def scene_detect(video, threshold, min_frames, max_frames):
  from scenedetect.video_manager import VideoManager
  from scenedetect.scene_manager import SceneManager
  from scenedetect.detectors import ContentDetector

  video_manager = VideoManager([video])
  scene_manager = SceneManager()
  scene_manager.add_detector(ContentDetector(threshold=threshold))
  base_timecode = video_manager.get_base_timecode()

  video_manager.set_duration()
  video_manager.set_downscale_factor()
  video_manager.start()

  scene_manager.detect_scenes(frame_source=video_manager, show_progress=True)
  scene_list = scene_manager.get_scene_list(base_timecode)

  scenes = [int(scene[0].get_frames()) for scene in scene_list][1:]

  final_scenes = []
  last_scene = 0
  previous_scene = scenes[0]
  for scene in scenes:
    if scene - last_scene >= max_frames and previous_scene - last_scene > min_frames:
      final_scenes.append(str(previous_scene))
      last_scene = previous_scene
    previous_scene = scene

  scenes = ",".join(final_scenes)

  return scenes

def parse_time(search):
  return int(search.group(1)) * 60 * 60 + int(search.group(2)) * 60 + int(search.group(3)) + float("." + search.group(4))

def print_progress(n, total, size=20, suffix=""):
  return f"{int(100 * n / total):3d}% {n}/{total} {suffix}"

def get_frames(input, fast=True):
  cmd = ["ffmpeg", "-hide_banner", "-i", input, "-map", "0:v:0"]
  if fast:
    cmd.extend(["-c", "copy",])
  cmd.extend(["-f", "null", "-"])
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  matches = re.findall(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8"))
  return int(matches[-1])

@contextlib.contextmanager
def tmp_file(mode, content, suffix=""):
  try:
    file = NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    file.write(content)
    file.flush()
    tmp_name = file.name
    file.close()
    yield tmp_name
  finally:
    os.unlink(tmp_name)

def split(video, path_split, threshold, min_frames, max_frames):
  frames = scene_detect(video, threshold, min_frames, max_frames)

  os.makedirs(path_split, exist_ok=True)

  cmd = [
    "ffmpeg", "-y",
    "-i", video,
    "-map", "0:v:0",
    "-an",
    "-c", "copy",
    #"-crf", "13",
    "-avoid_negative_ts", "1"
  ]
  
  if len(frames) > 0:
    cmd.extend([
      "-f", "segment",
      "-segment_frames", frames
    ])

  cmd.append(os.path.join(path_split, "%05d.mkv"))

  ffmpeg(cmd, None)
  
def ffmpeg(cmd, cb):
  pipe = subprocess.Popen(cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    universal_newlines = True)

  try:
    while True:
      line = pipe.stdout.readline().strip()

      if len(line) == 0 and pipe.poll() is not None:
        break

      if not cb: continue
      matches = re.findall(r"frame= *([^ ]+?) ", line)
      if matches:
        cb(int(matches[-1]))

  except KeyboardInterrupt as e:
    pipe.kill()
    raise e