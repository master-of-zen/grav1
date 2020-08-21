import logging
from threading import Thread, Event

class Logger(logging.Handler):
  def __init__(self):
    super(Logger, self).__init__()
    self.cr = {}
    self.last_cr = None
    self.save_event = Event()
    Thread(target=self._save, daemon=True).start()

  def _save(self):
    while self.save_event.wait():
      # TODO: save to file
      self.save_event.clear()

  def format(self, record):
    msg = [record.msg] + [str(s) for s in record.args]
    msg = " ".join(msg)
    return msg, f"[{record.levelname.lower()}] {msg}"

  def emit(self, record):
    msg, formatted = self.format(record)

    if self.last_cr and self.last_cr != record.levelname:
      print()

    if "cr" in dir(record) and record.cr:
      print(formatted, end="\r")
      self.cr[record.levelname] = msg
      self.last_cr = record.levelname
    else:
      if record.levelname in self.cr:
        print()
        del self.cr[record.levelname]
      print(formatted)
      self.last_cr = None

    # save this
    msg = {
      "msg": msg,
      "created": record.created
    }

    self.save_event.set()

NET = 21

def setup():
  logging.addLevelName(NET, "NET")
  
  root = logging.getLogger()
  root.addHandler(Logger())
  root.setLevel(20)
