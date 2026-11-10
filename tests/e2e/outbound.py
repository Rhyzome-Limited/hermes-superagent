import sys
from pathlib import Path

path, channel, text = sys.argv[1:4]
with Path(path).open("a", encoding="utf-8") as sent:
    one_line = text.replace("\n", "\\n")  # a message keeps one line in the file, whatever lines it holds
    sent.write(f"{channel}\t{one_line}\n")
