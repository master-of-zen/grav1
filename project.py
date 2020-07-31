import os, json, time, subprocess, re, logging
from threading import Thread, Event

from grav1ty.split import split, verify_split
from grav1ty.util import ffmpeg, get_frames
from util import tmp_file

from actions import actions

from logger import NET

class Projects:
  def __init__(self, working_dir):
    self.projects = {}
    self.working_dir = working_dir
    self.path_projects = os.path.join(working_dir, "projects.json")
    self.path_scenes = os.path.join(working_dir, "scenes")
    self.path_jobs = os.path.join(working_dir, "jobs")

    self.actions = actions

    self.action_queue = []
    self.action_lock = Event()
    Thread(target=self.action_loop, daemon=True).start()
    self.telemetry = {"encodes": [], "fph": 0, "fph_time": 0}

  def action_loop(self):
    while self.action_lock.wait():
      while len(self.action_queue) > 0:
        self.action_queue.pop(0)()
        self.save_projects()

      self.action_lock.clear()
  
  def values(self):
    return self.projects.values()

  def add_action(self, action):
    self.action_queue.append(action)

    if len(self.action_queue) > 0:
      self.action_lock.set()

  def project_on_complete(self, project):
    self.add_action(lambda: actions[project.action](self, project))

  def add(self, project, action="", save=True):
    logging.info("added project", project.projectid)
    project.projects = self

    if action:
      project.action = action
      project.on_complete = self.project_on_complete

    self.projects[project.projectid] = project
    if save:
      self.save_projects()

    if project.start():
      self.add_action(project.split)

  def get_job(self, skip_jobs):
    all_jobs = []

    for pid in self.projects:
      project = self.projects[pid]
      all_jobs.extend(project.jobs.values())

    all_jobs = [job for job in all_jobs if not any(job.scene == job2["scene"] and str(job.project.projectid) == str(job2["projectid"]) for job2 in skip_jobs)]
    all_jobs = sorted(all_jobs, key=lambda job: (job.project.priority, len(job.workers), -job.frames))

    return all_jobs[0] if len(all_jobs) > 0 else None

  def hit(self, frames):
    now = time.time()
    self.telemetry["encodes"] = list(filter(lambda x: (now - x[1]) < 3600, self.telemetry["encodes"]))
    self.telemetry["encodes"].append((frames, now))

    self.telemetry["fph"] = sum([x[0] for x in self.telemetry["encodes"]])
    self.telemetry["fph_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.telemetry["encodes"][-1][1]))

  def check_job(self, projectid, client, encoder, encoder_params, ffmpeg_params, scene_number, file):
    if projectid not in self.projects:
      logging.info("project not found", projectid)
      return "project not found"

    project = self.projects[projectid]

    if scene_number not in project.jobs:
      logging.info("job not found", projectid, scene_number)
      return "job not found"

    job = project.jobs[scene_number]
    scene = project.scenes[scene_number]

    if client in job.workers:
      job.workers.remove(client)
    
    if job.encoder_params != encoder_params or job.ffmpeg_params != ffmpeg_params or job.encoder != encoder:
      logging.log(NET, "discard from", client, projectid, scene_number, "bad params")
      return "bad params"

    encoded = os.path.join(project.path_encode, job.encoded_filename)

    if scene["filesize"] > 0:
      logging.log(NET, "discard from", client, projectid, scene_number, "already done")
      return "already done"

    os.makedirs(project.path_encode, exist_ok=True)
    file.save(encoded)
    
    if os.stat(encoded).st_size == 0:
      logging.log(NET, "discard from", client, projectid, scene_number, "bad upload")
      return "bad upload"
    
    if job.encoder == "aom":
      dav1d = subprocess.run([
        "dav1d",
        "-i", encoded,
        "-o", "/dev/null",
        "--framethreads", "1",
        "--tilethreads", "16"
      ], capture_output=True)

      if dav1d.returncode == 1:
        logging.log(NET, "discard from", client, projectid, scene_number, "dav1d decode error")
        return "bad encode"
      
      encoded_frames = int(re.search(r"Decoded [0-9]+/([0-9]+) frames", dav1d.stdout.decode("utf-8") + dav1d.stderr.decode("utf-8")).group(1))
    else:
      encoded_frames = get_frames(encoded)

    if scene["frames"] != encoded_frames:
      os.remove(encoded)
      logging.log(NET, "discard from", client, projectid, scene_number, "frame mismatch", encoded_frames, "/", scene["frames"])
      return "frame mismatch"

    scene["filesize"] = os.stat(encoded).st_size

    if client in job.workers:
      project.encoded_frames += scene["frames"]
      
    del project.jobs[scene_number]

    logging.log(NET, "recv", projectid, scene_number, "from", client)
    self.hit(scene["frames"])

    self.save_projects()

    if len(project.jobs) == 0 and project.get_frames() == project.total_frames:
      logging.info("done", projectid)
      self.add_action(project.complete)
      
    return "saved"

  def __len__(self):
    return len(self.projects)

  def __getitem__(self, key):
    return self.projects[key]

  def __contains__(self, key):
    return key in self.projects

  def __delitem__(self, key):
    if key in self.projects:
      del self.projects[key]
    self.save_projects()

  def save_projects(self):
    os.makedirs(os.path.join(self.working_dir, "scenes"), exist_ok=True)
    
    dict_projects = {}
    for project in self.projects.values():
      dict_projects[project.projectid] = {
        "priority": project.priority,
        "path_in": project.path_in,
        "encoder_params": project.encoder_params,
        "ffmpeg_params": project.ffmpeg_params,
        "min_frames": project.min_frames,
        "max_frames": project.max_frames,
        "encoder": project.encoder,
        "input_frames": project.input_total_frames,
        "on_complete": project.action
      }
      json.dump(project.scenes, open(os.path.join(self.path_scenes, f"{project.projectid}.json"), "w+"), indent=2)
    
    json.dump(dict_projects, open(self.path_projects, "w+"), indent=2)
    json.dump(self.userstats, open("stats.json", "w+"), indent=2)

  def load_projects(self):
    if not os.path.isfile("projects.json"): return
    projects = json.load(open("projects.json", "r"))
    for pid in projects:
      project_data = projects[pid]

      try:
        project = Project(
          project_data["path_in"],
          self.path_jobs, 
          project_data["encoder"],
          project_data["encoder_params"],
          project_data["ffmpeg_params"] if "ffmpeg_params" in project_data else "",
          project_data["min_frames"] if "min_frames" in project_data else -1,
          project_data["max_frames"] if "max_frames" in project_data else -1,
          json.load(open(os.path.join(self.path_scenes, f"{pid}.json"), "r")) if os.path.isfile(os.path.join(self.path_scenes, f"{pid}.json")) else {},
          project_data["input_frames"] if "input_frames" in project_data else 0,
          project_data["priority"] if "priority" in project_data else 0,
          pid
        )
      except:
        logging.info("Failed to load project", pid)
        continue

      self.add(project, project_data["on_complete"] if "on_complete" in project_data else "", save=False)

class Project:
  def __init__(self, filename, path, encoder, encoder_params, ffmpeg_params="", min_frames=-1, max_frames=-1, scenes={}, total_frames=0, priority=0, id=0):
    self.projectid = id or str(time.time())
    self.path_in = filename
    self.path_out = os.path.join(path, self.projectid, "completed.webm")
    self.path_split = os.path.join(path, self.projectid, "split")
    self.path_encode = os.path.join(path, self.projectid, "encode")
    self.status = "starting"
    self.jobs = {}
    self.min_frames = min_frames
    self.max_frames = max_frames
    self.encoder = encoder
    self.encoder_params = encoder_params
    self.ffmpeg_params = ffmpeg_params
    self.scenes = scenes
    self.total_jobs = 0
    self.priority = priority
    self.stopped = False
    self.input_total_frames = total_frames
    
    self.total_frames = 0

    self.encoded_frames = 0
    self.encode_start = None

    self.action = ""
    self.on_complete = None

    self.projects = None
  
  def get_frames(self):
    return sum([self.scenes[scene]["frames"] for scene in self.scenes if self.scenes[scene]["filesize"] != 0])

  def start(self):
    if not os.path.isdir(self.path_split) or len(os.listdir(self.path_split)) == 0:
      return True

    self.total_jobs = len(self.scenes)

    if os.path.isdir(self.path_encode):
      self.set_status("getting resume data")

    for scene in self.scenes:
      file_ivf = os.path.join(self.path_encode, self.get_encoded_filename(scene))
      self.scenes[scene]["filesize"] = os.stat(file_ivf).st_size if os.path.isfile(file_ivf) else 0
      self.total_frames += self.scenes[scene]["frames"]

    logging.info(self.projectid, "loaded")

    if self.stopped: return
    
    if self.input_total_frames == self.total_frames:
      for scene in self.scenes:
        if self.scenes[scene]["filesize"] > 0 or "bad" in self.scenes[scene]:
          continue

        encoded_filename = self.get_encoded_filename(scene)

        scene_setting = self.encoder_params
        scene_setting_ffmpeg = self.ffmpeg_params

        self.jobs[scene] = Job(
          self,
          scene,
          self.encoder,
          os.path.join(self.path_split, self.scenes[scene]["segment"]),
          encoded_filename,
          scene_setting,
          scene_setting_ffmpeg,
          self.scenes[scene]["start"],
          self.scenes[scene]["frames"]
        )

      self.set_status("ready")
    else:
      logging.info(self.projectid, "total frame mismatch", self.total_frames, self.input_total_frames)
      self.set_status("total frame mismatch")

    if os.path.isfile(self.path_out):
      self.set_status("complete")
    else:
      self.complete()

  def split(self):
    if self.stopped: return
    
    self.set_status("splitting")
    logging.info(self.projectid, "splitting")
    self.scenes, self.input_total_frames, segments = split(
      self.path_in,
      self.path_split,
      self.min_frames,
      self.max_frames,
      cb=lambda message, cr=False: logging.info(self.projectid, message, extra={"cr": cr})
    )

    logging.info(self.projectid, "verifying split")
    self.set_status("verifying split")
    verify_split(
      self.path_in,
      self.path_split,
      segments,
      cb=lambda message, cr=False: logging.info(self.projectid, message, extra={"cr": cr})
    )

    self.projects.save_projects()
    self.start()

  def complete(self):
    if len(self.jobs) == 0 and self.get_frames() == self.total_frames:
      self.set_status("done! joining files")
      self.concat()
      self.set_status("complete")
      logging.info(self.projectid, "completed")
      if self.on_complete:
        self.on_complete(self)

  def set_status(self, msg):
    self.status = msg

  def get_encoded_filename(self, scene_n):
    return f"{scene_n}.ivf"

  def concat(self):
    logging.info(self.projectid, "concat")
    keys = list(self.scenes.keys())
    keys.sort()
    scenes = [os.path.join(self.path_encode, self.get_encoded_filename(os.path.splitext(scene)[0])).replace("\\", "/") for scene in keys]
    content = "\n".join([f"file '{scene}'" for scene in scenes])
    with tmp_file("w", content) as file:
      cmd = f"ffmpeg -hide_banner -f concat -safe 0 -y -i".split(" ")
      cmd.extend([file, "-c", "copy", self.path_out])
      ffmpeg(cmd, lambda x: (self.set_status(f"concat {x}/{self.total_frames}"), logging.info(self.projectid, "concat", f"{x}/{self.total_frames}", extra={"cr": True})))

class Job:
  def __init__(self, project, scene, encoder, path, encoded_filename, encoder_params, ffmpeg_params, start, frames):
    self.project = project
    self.scene = scene
    self.encoder = encoder
    self.filename = os.path.basename(path)
    self.path = path
    self.encoded_filename = encoded_filename
    self.encoder_params = encoder_params
    self.ffmpeg_params = ffmpeg_params
    self.workers = []
    self.start = start
    self.frames = frames
