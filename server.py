#!/usr/bin/env python3

import os, re, json
from time import time
from threading import Thread

from flask import Flask, request, send_file, make_response, send_from_directory
from flask_cors import cross_origin
from wsgiserver import WSGIServer

from util import get_frames, parse_time, split, tmp_file, ffmpeg

path_split = "jobs/{}/split"
path_encode = "jobs/{}/encode"
path_in = "inputfiles"
path_out = "jobs/{}/completed.webm"

re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}).(\d{2})", re.U)
re_position = re.compile(r".*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", re.U)

class Project:
  def __init__(self, filename, encoder, encoder_params, threshold, min_frames, max_frames, scenes, total_frames=0, priority=0, id=0):
    self.projectid = id or int(time())
    self.path_in = filename
    self.path_out = path_out.format(self.projectid)
    self.path_split = path_split.format(self.projectid)
    self.path_encode = path_encode.format(self.projectid)
    self.log = []
    self.status = "starting"
    self.jobs = {}
    self.threshold = threshold
    self.min_frames = min_frames
    self.max_frames = max_frames
    self.encoder = encoder
    self.encoder_params = encoder_params
    self.scenes = scenes
    self.total_jobs = 0
    self.priority = priority
    self.stopped = False
    self.input_total_frames = total_frames
    
    self.total_frames = 0

    self.frames = 0
    self.encoded_frames = 0
    self.encode_start = None
    self.fps = 0
  
  def count_frames(self, scene):
    global ffmpeg_pool
    scene_n = str(os.path.splitext(scene)[0])

    if not self.scenes[scene_n]["frames"]:
      num_frames = get_frames(os.path.join(self.path_split, scene))

      self.total_frames += num_frames
      self.scenes[scene_n]["frames"] = num_frames
      
    encoded_filename = self.get_encoded_filename(scene_n)

    file_ivf = os.path.join(self.path_encode, encoded_filename)

    if os.path.isfile(file_ivf):
      self.scenes[scene_n]["filesize"] = os.stat(file_ivf).st_size
      self.frames += num_frames

    ffmpeg_pool -= 1

  def verify_split(self):
    scene_filenames = os.listdir(self.path_split)

    for i, scene in enumerate(scene_filenames, start=1):
      scene_n = str(os.path.splitext(scene)[0])
      num_frames = get_frames(os.path.join(self.path_split, scene))
      num_frames_slow = get_frames(os.path.join(self.path_split, scene), False)
      
      if num_frames_slow != num_frames:
        print("bad framecount", self.projectid, scene, "supposed to be:", num_frames, "got:", num_frames_slow)
        cmd = ["ffmpeg", "-i", self.path_in, "-vf", f"select=gte(n\\,{self.total_frames})", "-frames:v", str(num_frames)]
        cmd.extend(f"-crf 1 -qmin 0 -qmax 1 -an -y".split(" "))
        cmd.append(os.path.join(self.path_split, scene))
        ffmpeg(cmd, None)

        if get_frames(os.path.join(self.path_split, scene), False) == num_frames:
          print("corrected", self.projectid, scene)
        else:
          print("failed to correct", self.projectid, scene)
          self.scenes[scene_n]["bad"] = f"bad framecount, supposed to be: {num_frames}, got: {num_frames_slow}"
      
      self.total_frames += num_frames
      self.scenes[scene_n] = {
        "filesize": 0,
        "frames": num_frames,
        "encoder_params": ""
      }
      self.set_status(f"verifying split {i}/{len(scene_filenames)}")

  def start(self):
    global ffmpeg_pool
    
    if not self.input_total_frames:
      self.input_total_frames = get_frames(self.path_in)

    if not os.path.isdir(self.path_split) or len(os.listdir(self.path_split)) == 0:
      self.set_status("splitting", True)
      split(self.path_in, self.path_split, self.threshold, self.min_frames, self.max_frames)
      self.set_status("verifying split", True)
      self.verify_split()

    scene_filenames = os.listdir(self.path_split)

    self.total_jobs = len(scene_filenames)

    if os.path.isdir(self.path_encode):
      self.set_status("getting resume data", True)
    
    ffmpeg_threads = []

    for scene in scene_filenames:
      scene_n = str(os.path.splitext(scene)[0])
      while ffmpeg_pool >= 4:
        pass
      if self.stopped: return
      
      if scene_n not in self.scenes:
        self.scenes[scene_n] = {
          "filesize": 0,
          "frames": 0,
          "encoder_params": ""
        }

      if self.scenes[scene_n]["filesize"]:
        self.frames += self.scenes[scene_n]["frames"]

      if self.scenes[scene_n]["frames"]:
        self.total_frames += self.scenes[scene_n]["frames"]

      if self.scenes[scene_n]["frames"] == 0 or self.scenes[scene_n]["filesize"] == 0:
        ffmpeg_pool += 1
        t = Thread(target=self.count_frames, args=(scene,), daemon=True)
        t.start()
        ffmpeg_threads.append(t)

    for t in ffmpeg_threads:
      t.join()
    
    save_projects()
    print("done loading", self.projectid)

    if self.stopped: return
    
    if self.input_total_frames == self.total_frames:
      for scene in scene_filenames:
        scene_n = str(os.path.splitext(scene)[0])
        if self.scenes[scene_n]["filesize"] > 0 or "bad" in self.scenes[scene_n]:
          continue

        encoded_filename = self.get_encoded_filename(scene_n)

        scene_setting = self.scenes[scene_n]["encoder_params"] if self.scenes[scene_n]["encoder_params"] else self.encoder_params

        self.jobs[scene_n] = Job(
          self.encoder,
          os.path.join(self.path_split, scene),
          encoded_filename,
          self.projectid,
          self.priority,
          scene_setting,
          self.scenes[scene_n]["frames"]
        )

      self.set_status("ready", True)
    else:
      print("total frame mismatch")
      self.set_status("total frame mismatch")

    if os.path.isfile(self.path_out):
      self.set_status("complete")
    else:
      self.complete()

  def complete(self):
    if len(self.jobs) == 0 and self.frames == self.total_frames:
      self.set_status("done! joining files", True)
      self.concat()
      self.set_status("complete")

  def update_progress(self):
    if self.encode_start: self.fps = self.encoded_frames / max(time() - self.encode_start, 1)
    else: self.fps = 0

  def set_status(self, msg, log=False):
    if log:
      self.log.append(msg)
    self.status = msg

  def get_encoded_filename(self, scene_n):
    return scene_n + ".ivf"

  def concat(self):
    keys = list(self.scenes.keys())
    keys.sort()
    scenes = [os.path.join(self.path_encode, self.get_encoded_filename(os.path.splitext(scene)[0])).replace("\\", "/") for scene in keys]
    content = "\n".join([f"file '{scene}'" for scene in scenes])
    with tmp_file("w", content) as file:
      cmd = f"ffmpeg -hide_banner -f concat -safe 0 -y -i".split(" ")
      cmd.extend([file, "-c", "copy", self.path_out])
      ffmpeg(cmd, lambda x: self.set_status(f"concat {x}, {self.total_frames}"))

class Job:
  def __init__(self, encoder, path, encoded_filename, projectid, priority, encoder_params, frames):
    self.encoder = encoder
    self.filename = os.path.basename(path)
    self.scene = os.path.splitext(self.filename)[0]
    self.path = path
    self.encoded_filename = encoded_filename
    self.encoder_params = encoder_params
    self.workers = []
    self.projectid = projectid
    self.priority = priority
    self.frames = frames

def save_projects():
  rtn = {}
  for pid in projects:
    project = projects[pid]
    p = {}
    p["priority"] = project.priority
    p["path_in"] = project.path_in
    p["encoder_params"] = project.encoder_params
    p["threshold"] = project.threshold
    p["min_frames"] = project.min_frames
    p["max_frames"] = project.max_frames
    p["scenes"] = project.scenes
    p["encoder"] = project.encoder
    p["input_frames"] = project.input_total_frames
    rtn[pid] = p

  json.dump(rtn, open("projects.json", "w+"), indent=2)

def load_projects():
  if not os.path.isfile("projects.json"): return
  ps = json.load(open("projects.json", "r"))
  for pid in ps:
    p = ps[pid]
    project = Project(
      p["path_in"],
      p["encoder"],
      p["encoder_params"],
      p["threshold"],
      p["min_frames"],
      p["max_frames"],
      p["scenes"],
      p["input_frames"],
      p["priority"],
      pid)
    projects[pid] = project
  
  for pid in projects:
    projects[pid].start()

app = Flask(__name__)

@app.route("/scene/<projectid>/<scene>", methods=["GET"])
@cross_origin()
def get_scene(projectid, scene):
  if projectid in projects:
    return send_from_directory(projects[projectid].path_encode, scene)
  return "", 404

@app.route("/completed/<projectid>", methods=["GET"])
@cross_origin()
def get_completed(projectid):
  if projectid in projects:
    return send_file(projects[projectid].path_out)
  return "", 404

@app.route("/api/get_projects", methods=["GET"])
@cross_origin()
def get_projects():
  rtn = []
  for pid in projects:
    project = projects[pid]
    p = {}
    p["projectid"] = pid
    p["input"] = project.path_in
    p["frames"] = project.frames
    p["total_frames"] = project.input_total_frames
    p["fps"] = project.fps
    p["jobs"] = len(project.jobs)
    p["total_jobs"] = project.total_jobs
    p["status"] = project.status
    p["encoder_params"] = project.encoder_params
    p["encoder"] = project.encoder
    p["scenes"] = project.scenes
    p["priority"] = project.priority
    p["workers"] = [job for job in project.jobs if len(project.jobs[job].workers) > 0]

    rtn.append(p)
  return json.dumps(rtn)

@app.route("/api/get_job/<jobs>", methods=["GET"])
def get_job(jobs):
  jobs = json.loads(jobs)

  all_jobs = []

  for pid in projects:
    project = projects[pid]
    all_jobs.extend(project.jobs.values())

  all_jobs = [job for job in all_jobs if not any(job.scene == job2["scene"] and str(job.projectid) == str(job2["projectid"]) for job2 in jobs)]
  all_jobs = sorted(all_jobs, key= lambda x: (x.priority, len(x.workers), x.frames))
  if len(all_jobs) > 0:
    new_job = all_jobs[0]
    
    workerid = f'{request.environ["REMOTE_ADDR"]}:{request.environ["REMOTE_PORT"]}'
    new_job.workers.append(workerid)

    if not project.encode_start:
      project.encode_start = time()

    print("sent", new_job.projectid, new_job.scene, "to", workerid)

    resp = make_response(send_file(new_job.path))
    resp.headers["success"] = "1"
    resp.headers["projectid"] = new_job.projectid
    resp.headers["filename"] = new_job.filename
    resp.headers["scene"] = new_job.scene
    resp.headers["id"] = workerid
    resp.headers["encoder"] = new_job.encoder
    resp.headers["encoder_params"] = new_job.encoder_params
    return resp
  else:
    resp = make_response("")
    resp.headers["success"] = "0"
    return resp

@app.route("/finish_job", methods=["POST"])
def receive():
  sender = request.form["id"]
  encoder = request.form["encoder"]
  encoder_params = request.form["encoder_params"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])
  file = request.files["file"]

  if projectid not in projects:
    return "project not found", 200

  project = projects[projectid]

  if scene_number not in project.jobs:
    return "job not found", 200

  job = project.jobs[scene_number]
  scene = project.scenes[scene_number]

  if job.encoder_params != encoder_params:
    if sender in job.workers:
      job.workers.remove(sender)
    print("discard", projectid, scene_number, "bad params")
    return "bad params", 200

  encoded = os.path.join(project.path_encode, job.encoded_filename)
  
  if os.path.isfile(encoded):
    print("discard", projectid, scene_number, "already done")
    return "already done", 200

  os.makedirs(project.path_encode, exist_ok=True)
  file.save(encoded)
  
  encoded_frames = get_frames(encoded)
  if scene["frames"] != encoded_frames:
    os.remove(encoded)
    if sender in job.workers:
      job.workers.remove(sender)
    print("discard", projectid, scene_number, "frame mismatch", encoded_frames, scene["frames"])
    return "bad framecount", 200

  scene["filesize"] = os.stat(encoded).st_size

  project.frames += scene["frames"]
  if sender in job.workers:
    project.encoded_frames += scene["frames"]
    
  del project.jobs[scene_number]

  print("recv", projectid, scene_number, "from", sender)

  project.update_progress()

  if len(project.jobs) == 0 and project.frames == project.total_frames:
    print("done", projectid)
    Thread(target=lambda: project.complete(), daemon=True).start()
    
  return "saved", 200

@app.route("/api/list_directory", methods=["GET"])
@cross_origin()
def list_directory():
  return json.dumps(os.listdir("inputfiles"))

@app.route("/api/delete_project/<projectid>", methods=["POST"])
@cross_origin()
def delete_project(projectid):
  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  projects[projectid].stopped = True
  del projects[projectid]

  return json.dumps({"success": True})

@app.route("/api/modify/<projectid>", methods=["POST"])
@cross_origin()
def modify_project(projectid):
  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  project = projects[projectid]

  changes = request.json
  if "priority" in changes:
    project.priority = int(changes["priority"])

  return json.dumps({"success": True})

@app.route("/api/add_project", methods=["POST"])
@cross_origin()
def add_project():
  content = request.json

  for input_file in content["input"]:
    if not os.path.isfile(input_file): continue

    new_project = Project(
      input_file,
      content["encoder"],
      content["encoder_params"],
      content["threshold"],
      content["min_frames"],
      content["max_frames"],
      {})

    projects[new_project.projectid] = new_project

    Thread(target=lambda: new_project.start(), daemon=True).start()

  save_projects()
  return json.dumps({"success": True})

ffmpeg_pool = 0
if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--port", dest="port", default=7899)
  args = parser.parse_args()

  projects = {}
  Thread(target=load_projects, daemon=True).start()

  print("listening on port", args.port)
  WSGIServer(app, port=args.port).start()
