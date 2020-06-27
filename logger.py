import time

class Logger:
  def __init__(self):
    self.messages = {}
    self.cursors = {}
    self.cr = False
  
  def default(self, *argv, cr=False):
    self.add("default", *argv, cr=cr)

  def add(self, cat, *argv, cr=False):
    message = " ".join([str(arg) for arg in argv])

    if cat in self.messages:
      if cr and self.cursors[cat] != len(self.messages[cat]):
        self.messages[cat][self.cursors[cat]] = (time.time(), message)
      else:
        self.messages[cat].append((time.time(), message))
    else:
      self.messages[cat] = [(time.time(), message)]

    if self.cr and cat in self.cursors and not cr and self.cursors[cat] < len(self.messages[cat]) - 1:
      print()

    if cr:
      print(f"[{cat}]", message, end="\r")
    else:
      print(f"[{cat}]", message)

    self.cursors[cat] = len(self.messages[cat]) - (1 if cr else 0)
    self.cr = cr
  