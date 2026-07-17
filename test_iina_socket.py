import os
import sys
import time

from videolink import open_in_iina


def main() -> int:
    """Manual player IPC smoke test; excluded from unittest import side effects."""
    socket_path = "/tmp/test-iina-sock-" + str(int(time.time())) + ".sock"
    print("Socket path:", socket_path)
    url = "https://vcdn02.spiegel.de/videos/Wa1zjie2-b8Tmknhy.mp4"
    opened = open_in_iina(url, ipc_path=socket_path)
    print("Opened:", opened)
    for i in range(100):
        if os.path.exists(socket_path):
            print("Found socket after", i * 0.1, "seconds!")
            return 0
        time.sleep(0.1)
    print("Socket not found!")
    return 1


if __name__ == "__main__":
    sys.exit(main())
