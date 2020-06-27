import os, json, time
from threading import Thread, Event

from split import split, verify_split
from util import tmp_file, ffmpeg

class Projects:
  def __init__(self, logger):
    self.logger = logger
    self.projects = {}
    self.action_queue = []
    self.action_lock = Event()
    Thread(target=self.action_loop, daemon=True).start()
    self.monitor = None
  
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

  def add(self, project):
    project.logger = self.logger
    self.projects[project.projectid] = project
    self.save_projects()

    if project.start():
      self.add_action(project.split)

  def get_job(self, skip_jobs):
    all_jobs = []

    for pid in self.projects:
      project = self.projects[pid]
      all_jobs.extend(project.jobs.values())

    all_jobs = [job for job in all_jobs if not any(job.scene == job2["scene"] and str(job.projectid) == str(job2["projectid"]) for job2 in skip_jobs)]
    all_jobs = sorted(all_jobs, key=lambda x: (x.priority, len(x.workers), x.frames))

    return all_jobs[0] if len(all_jobs) > 0 else None

  def verify_encode(self, encode):
    pass

  def __getitem__(self, key):
    return self.projects[key]

  def __contains__(self, key):
    return key in self.projects

  def __delitem__(self, key):
    if key in self.projects:
      del self.projects[key]
    self.save_projects()

  def save_projects(self):
    dict_projects = {}
    for project in self.projects.values():
      dict_projects[project.projectid] = {
        "priority": project.priority,
        "path_in": project.path_in,
        "encoder_params": project.encoder_params,
        "min_frames": project.min_frames,
        "max_frames": project.max_frames,
        "encoder": project.encoder,
        "input_frames": project.input_total_frames,
        "from_monitor": project.on_complete is not None
      }
      json.dump(project.scenes, open(f"scenes/{project.projectid}.json", "w+"), indent=2)
    
    json.dump(dict_projects, open("projects.json", "w+"), indent=2)

  def load_projects(self, path_out, path_split, path_encode):
    if not os.path.isfile("projects.json"): return
    projects = json.load(open("projects.json", "r"))
    for pid in projects:
      project = projects[pid]

      self.add(Project(
        project["path_in"],
        path_out, path_split, path_encode, 
        project["encoder"],
        project["encoder_params"],
        project["min_frames"],
        project["max_frames"],
        json.load(open(f"scenes/{pid}.json")) if os.path.isfile(f"scenes/{pid}.json") else {},
        project["input_frames"],
        project["priority"],
        pid,
        self.monitor.on_complete if self.monitor and "from_monitor" in project and project["from_monitor"] else None
      ))

class Project:
  def __init__(self, filename, path_out, path_split, path_encode, encoder, encoder_params, min_frames, max_frames, scenes={}, total_frames=0, priority=0, id=0, on_complete=None):
    self.projectid = id or str(time.time())
    self.path_in = filename
    self.path_out = path_out.format(self.projectid)
    self.path_split = path_split.format(self.projectid)
    self.path_encode = path_encode.format(self.projectid)
    self.status = "starting"
    self.jobs = {}
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

    self.encoded_frames = 0
    self.encode_start = None
    self.fps = 0

    self.on_complete = on_complete
    self.logger = None
  
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

    self.logger.default(self.projectid, "loaded")

    if self.stopped: return
    
    if self.input_total_frames == self.total_frames:
      for scene in self.scenes:
        if self.scenes[scene]["filesize"] > 0 or "bad" in self.scenes[scene]:
          continue

        encoded_filename = self.get_encoded_filename(scene)

        scene_setting = self.encoder_params

        self.jobs[scene] = Job(
          self.projectid,
          scene,
          self.encoder,
          os.path.join(self.path_split, self.scenes[scene]["segment"]),
          encoded_filename,
          self.priority,
          scene_setting,
          self.scenes[scene]["start"],
          self.scenes[scene]["frames"]
        )

      self.set_status("ready")
    else:
      self.logger.default(self.projectid, "total frame mismatch", self.total_frames, self.input_total_frames)
      self.set_status("total frame mismatch")

    if os.path.isfile(self.path_out):
      self.set_status("complete")
    else:
      self.complete()

  def split(self):
    self.set_status("splitting")
    self.logger.default(self.projectid, "splitting")
    self.scenes, self.input_total_frames, segments = split(
      self.path_in,
      self.path_split,
      self.min_frames,
      self.max_frames,
      cb=lambda message, cr=False: self.logger.default(self.projectid, message, cr=cr)
    )
    self.set_status("verifying split")
    verify_split(
      self.path_in,
      self.path_split,
      segments,
      cb=lambda message, cr=False: self.logger.default(self.projectid, message, cr=cr)
    )

    self.start()

  def complete(self):
    if len(self.jobs) == 0 and self.get_frames() == self.total_frames:
      self.set_status("done! joining files")
      self.concat()
      self.set_status("complete")
      self.logger.default(self.projectid, "completed")
      if self.on_complete: self.on_complete(self)

  def update_progress(self):
    if self.encode_start: self.fps = self.encoded_frames / max(time() - self.encode_start, 1)
    else: self.fps = 0

  def set_status(self, msg):
    self.status = msg

  def get_encoded_filename(self, scene_n):
    return f"{scene_n}.ivf"

  def concat(self):
    self.logger.default(self.projectid, "concat")
    keys = list(self.scenes.keys())
    keys.sort()
    scenes = [os.path.join(self.path_encode, self.get_encoded_filename(os.path.splitext(scene)[0])).replace("\\", "/") for scene in keys]
    content = "\n".join([f"file '{scene}'" for scene in scenes])
    with tmp_file("w", content) as file:
      cmd = f"ffmpeg -hide_banner -f concat -safe 0 -y -i".split(" ")
      cmd.extend([file, "-c", "copy", self.path_out])
      ffmpeg(cmd, lambda x: (self.set_status(f"concat {x}/{self.total_frames}"), self.logger.default(self.projectid, f"concat {x}/{self.total_frames}", cr=True)))

class Job:
  def __init__(self, projectid, scene, encoder, path, encoded_filename, priority, encoder_params, start, frames):
    self.projectid = projectid
    self.scene = scene
    self.encoder = encoder
    self.filename = os.path.basename(path)
    self.path = path
    self.encoded_filename = encoded_filename
    self.encoder_params = encoder_params
    self.workers = []
    self.priority = priority
    self.start = start
    self.frames = frames
