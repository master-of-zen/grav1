#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, sys
from datetime import datetime
from tempfile import NamedTemporaryFile

path_split = "split"
path_encode = "encode"

def scene_detect(video, threshold, scene_factor):
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

  scenes = [str(scene[0].get_frames()) for scene in scene_list]

  scenes = ",".join(scenes[1:][::scene_factor])

  return scenes

def time2sec(search):
  return int(search.group(1)) * 60 * 60 + int(search.group(2)) * 60 + int(search.group(3)) + float("." + search.group(4))

def _split(video, frames, path_split):
  os.makedirs(path_split, exist_ok=True)

  cmd = [
    "ffmpeg", "-y",
    "-hide_banner",
    "-i", video,
    "-map", "0:v:0",
    "-an",
    "-c", "copy",
    "-avoid_negative_ts", "1"
  ]
  
  if len(frames) > 0:
    cmd.extend([
      "-f", "segment",
      "-segment_frames", frames
    ])

  cmd.append(os.path.join(path_split, "%05d.mkv"))

  try:
    pipe = subprocess.Popen(cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      universal_newlines = True)
    duration = 0

    while True:
      line = pipe.stdout.readline().strip()

      if len(line) == 0 and pipe.poll() is not None:
        break

      if not duration:
        match = re.search(r"Duration: (..):(..):(..)\.(..)", line)
        if match:
          duration = time2sec(match)
      else:
        match = re.search(r"time=(..):(..):(..)\.(..)", line)
        if match:
          time = time2sec(match)

          print_progress(time, duration)
    
    if pipe.returncode == 0:
      print_progress(duration, duration)
      print()
      return True

    return False
  except KeyboardInterrupt as e:
    pipe.kill()
    raise e

def split(video, path_split, threshold, scene_factor):
  if os.path.isfile("frames"):
    frames = open("frames", "r").read()
  else:
    frames = scene_detect(video, threshold, scene_factor)

    with open("frames", "w+") as file:
      file.write(frames)

  if _split(video, frames, path_split):
    os.remove("frames")
    return True
  
  return False

def print_progress(n, total, size=20, suffix=""):
  fill = "█" * int((n / total) * size)
  remaining = " " * (size - len(fill))
  return f"{int(100 * n / total):3d}%|{fill}{remaining}| {n}/{total} {suffix}"

def get_frames(input):
  cmd = f"ffmpeg -hide_banner -map 0:v:0 -c copy -f null {os.devnull} -i".split(" ")
  cmd.append(input)
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  return int(re.search(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8")).group(1))

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

class Job:
  def __init__(self, path):
    self.filename = os.path.basename(path)
    self.path = path
    self.encoded_name = os.path.splitext(self.filename)[0] + ".ivf"
    self.workers = []
    self.completed = False

class Server:
  def __init__(self, app, args):
    self.config = type("", (), {})
    self.config.encoder = "aomenc"
    self.config.encoder_params = args.encoder_params
    self.config.path_input = args.input
    self.config.path_output = args.target if args.target else f"{self.config.path_input}_av1.webm"

    self.last_message = ""

    self.frames = 0
    self.total_frames = get_frames(args.input)
    
    self.encode_start = None
    self.encoded_frames = 0

    self.jobs = {}

    if not os.path.isdir(path_split) or len(os.listdir(path_split)) == 0:
      split(args.input, path_split, args.threshold, args.scene_factor)
    
    self.scenes = os.listdir(path_split)

    self.total_scenes = len(self.scenes)

    print("getting resume data")

    for file in self.scenes:
      file_ivf = os.path.join(path_encode, os.path.splitext(file)[0]) + ".ivf"
      if os.path.isdir(path_encode) and os.path.isfile(file_ivf):
        self.frames += get_frames(file_ivf)
        self.print_progress()
        continue
      self.jobs[file] = Job(os.path.join(path_split, file))

      self.print_progress()

    if len(self.jobs) == 0 and self.frames == self.total_frames:
      print("done! joining files")
      self.concat()
      return None

  def print(self, *args):
    print(" "*len(self.last_message), end="\r")
    print(*args)
    self.print_progress()
    
  def print_progress(self):
    if self.encode_start:
      fps = self.encoded_frames / max((datetime.now() - self.encode_start).seconds, 1)
    else:
      fps = 0
    self.last_message = print_progress(self.frames, self.total_frames, suffix=f"{fps:.2f}fps {len(self.jobs)}/{self.total_scenes} scenes remaining")
    print(self.last_message, end="\r")

  def get_job(self):
    jobs = sorted(self.jobs.values(), key= lambda x: len(x.workers))
    if len(jobs) > 0: return jobs[0]
    else: return None

  def concat(self):
    get_encoded_path = lambda x: os.path.join(path_encode, os.path.splitext(x)[0] + ".ivf").replace("\\", "/")
    content = "\n".join([f"file '{get_encoded_path(file)}'" for file in self.scenes])
    with tmp_file("w", content) as file:
      cmd = f"ffmpeg -hide_banner -f concat -safe 0 -y -i".split(" ")
      cmd.extend([file, "-c", "copy", self.config.path_output])

      pipe = subprocess.Popen(cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines = True)

      try:
        while True:
          line = pipe.stdout.readline().strip()

          if len(line) == 0 and pipe.poll() is not None:
            break
          
          match = re.match(r"frame= *([^ ]+?) ", line)
          if match:
            print_progress(int(match.group(1)), self.total_frames)
      except KeyboardInterrupt as e:
        pipe.kill()
        raise e

def get_job():
  new_job = server.get_job()
  if not new_job:
    return "", 404

  id = request.environ["REMOTE_ADDR"] + ":" + request.environ["REMOTE_PORT"]
  new_job.workers.append(id)

  if not server.encode_start:
    server.encode_start = datetime.now()

  server.print("sent", new_job.filename, "to", id)

  resp = make_response(send_file(new_job.path))
  resp.headers["filename"] = new_job.filename
  resp.headers["id"] = id
  resp.headers["encoder_params"] = server.config.encoder_params
  return resp

def receive():
  id = request.form["id"]
  filename = request.form["filename"]
  file = request.files["file"]

  if filename not in server.jobs:
    return "bad", 200

  job = server.jobs[filename]
  
  encoded = os.path.join(path_encode, job.encoded_name)

  if os.path.isfile(encoded):
    server.print("disc", filename)
    return "already done", 200

  os.makedirs(path_encode, exist_ok=True)
  
  file.save(encoded)

  frames = get_frames(encoded)

  if frames != get_frames(job.path):
    os.remove(encoded)
    server.print("disc", filename)
    return "bad", 200

  server.frames += frames
  if id in job.workers:
    server.encoded_frames += frames
    
  del server.jobs[filename]

  server.print("recv", filename, "from", id)

  if len(server.jobs) == 0 and server.frames == server.total_frames:
    print()
    print("done! joining files")
    server.concat()

  return "saved", 200

def aom_encode(input, total_frames, encoder_params):
  output_filename = f"{input}.ivf"

  ffmpeg = f"ffmpeg -y -hide_banner -loglevel error -i {input} -strict -1 -pix_fmt yuv420p -f yuv4mpegpipe -".split(" ")

  aom = f"aomenc - --passes=2 --fpf={input}.log --threads=4 {encoder_params}".split(" ")

  cmd1 = aom.copy()
  cmd1.extend(["--pass=1", "-o", os.devnull])

  cmd2 = aom.copy()
  cmd2.extend(["--pass=2", "-o", output_filename])

  try:
    for cmd in [cmd1, cmd2]:
      print(" ".join(cmd))
      ffmpeg_pipe = subprocess.Popen(ffmpeg,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)

      pipe = subprocess.Popen(cmd,
        stdin=ffmpeg_pipe.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True)

      print_progress(0, total_frames)

      while True:
        line = pipe.stdout.readline().strip()

        if len(line) == 0 and pipe.poll() is not None:
          break

        match = re.search(r"frame *([^ ]+?)/", line)
        if match:
          print_progress(int(match.group(1)), total_frames)
      
      if pipe.returncode == 0:
        print()
      else:
        print()
        print("error!")
        return False

    if os.path.isfile(f"{input}.log"):
      os.remove(f"{input}.log")

    return output_filename
  except KeyboardInterrupt as e:
    pipe.kill()
    raise e

def client(host, vmaf_path):
  while True:
    try:
      r = requests.get(host + "/get_job")
      if r.status_code == 404:
        print("finished!")
        return
      job = type("", (), {})
      job.id = r.headers["id"]
      job.filename = r.headers["filename"]
      job.encoder_params = r.headers["encoder_params"]
      job.content = r.content
    except requests.exceptions.ConnectionError:
      print("server not found")
      sys.exit()

    if not job:
      print("finished!")
      return

    if len(vmaf_path) > 0:
      job.encoder_params = f"{job.encoder_params} --vmaf-model-path={vmaf_path}"

    print("received job", job.filename)
    
    with tmp_file("wb", job.content, job.filename) as file:
      output = aom_encode(file, get_frames(file), job.encoder_params)
      if output:
        print("Encoding complete! Uploading results...")
        with open(output, "rb") as file:
          files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
          requests.post(host + "/finish_job", data={"id": job.id, "filename": job.filename}, files=files)

        while os.path.isfile(output):
          try:
            os.remove(output)
          except:
            print("failed to delete")
            time.sleep(1)

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("-i", dest="input", default=None)
  parser.add_argument("target", type=str, default=None)
  parser.add_argument("--threshold", type=int, default=50)
  parser.add_argument("--scene-factor", type=int, default=1)
  parser.add_argument("--av1-options", dest="encoder_params", type=str, default=
    "--lag-in-frames=35 --auto-alt-ref=1 \
    -b 10 --aq-mode=2 --cpu-used=0 --end-usage=vbr --target-bitrate=10 -w 768 -h 432"
  )
  parser.add_argument("--vmaf-model-path", dest="vmaf_path", default="vmaf_v0.6.1.pkl" if os.name == "nt" else "")

  args = parser.parse_args()

  if args.input:
    from flask import Flask, request, send_file, make_response
    from wsgiserver import WSGIServer

    app = Flask(__name__)

    @app.route("/get_job", methods=["GET"])
    def _get_job():
      return get_job()

    @app.route("/finish_job", methods=["POST"])
    def _receive():
      return receive()

    server = Server(app, args)
    if server:
      server.print("ready for encoding")
      server.print("starting server")
      WSGIServer(app, port=7899).start()
  elif args.target:
    client(args.target, args.vmaf_path)
