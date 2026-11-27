# assemble_setup.py —— 拼装安装程序：外壳 + 载荷 + 24 字节页脚
#
# 用法：python setup\tools\assemble_setup.py <setup-shell.exe> <payload.7z> <输出.exe>
# 页脚（小端）：magic "BAKERSET"(8B) + Int64(外壳长度 = 载荷起点) + Int64(载荷长度)
# 拼完自检：页脚可解析、offset+length+24 == 文件大小、载荷起点是 7z 魔数。

import os
import struct
import sys

MAGIC = b'BAKERSET'


def main(shell_path, payload_path, out_path):
    with open(shell_path, 'rb') as f:
        shell = f.read()
    with open(payload_path, 'rb') as f:
        payload = f.read()

    if len(shell) < 1024:
        print('!! 外壳太小，可能不是 exe', file=sys.stderr)
        return 1
    if payload[:2] != b'7z':
        print('!! 载荷不是 7z: ' + repr(payload[:6]), file=sys.stderr)
        return 1

    offset = len(shell)
    length = len(payload)
    footer = MAGIC + struct.pack('<q', offset) + struct.pack('<q', length)

    with open(out_path, 'wb') as f:
        f.write(shell)
        f.write(payload)
        f.write(footer)

    # ---- 自检 ----
    size = os.path.getsize(out_path)
    assert offset + length + 24 == size, (offset, length, size)
    with open(out_path, 'rb') as f:
        f.seek(-24, os.SEEK_END)
        fb = f.read(24)
    assert fb[:8] == MAGIC, fb[:8]
    off = struct.unpack('<q', fb[8:16])[0]
    ln = struct.unpack('<q', fb[16:24])[0]
    assert off == offset and ln == length, (off, ln, offset, length)
    with open(out_path, 'rb') as f:
        f.seek(off)
        head = f.read(6)
    assert head[:2] == b'7z', head

    print('assembled: %s' % out_path)
    print('  shell   %d B' % offset)
    print('  payload %d B (%.1f MB)' % (length, length / 1048576))
    print('  total   %d B (%.1f MB)' % (size, size / 1048576))
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print('usage: python assemble_setup.py <shell.exe> <payload.7z> <out.exe>')
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
