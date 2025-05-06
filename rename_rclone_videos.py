import subprocess
import re
import os
import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# === 配置项 ===
REMOTES = {
    'quark': {
        'tv': ['tv'],
        'movie': ['movie']
    },
    'aliyundrive': {
        'tv': ['tv'],
        'movie': ['movie']
    }
}

VIDEO_EXTS = (
    '.mp4', '.mkv', '.avi', '.mov', '.m4v', '.wmv', '.flv', '.webm', '.ts',
    '.m2ts', '.mts', '.vob', '.rmvb', '.ogm', '.asf', '.3gp', '.iso',
    '.srt', '.ssa', '.ass', '.smi', '.sub', '.vtt', '.pgs', '.txt', '.mpl',
    '.dvb', '.ttml', '.nfo'
)

DRY_RUN = False
MAX_WORKERS = 4

# 自动定位日志路径为与当前脚本同目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "rename_rclone_videos.log")

# ✅ 清空日志文件（只运行一次，确保非追加模式）
with open(LOG_PATH, "w", encoding="utf-8") as f:
    f.write("")

def log(msg):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[ERROR] 写入日志失败: {e}")

# === 路径检查 ===
def remote_path_exists(remote, path):
    result = subprocess.run(['rclone', 'lsf', f'{remote}:{path}'], capture_output=True, text=True)
    return result.returncode == 0

def list_files(remote, base_dir):
    result = subprocess.run(['rclone', 'lsf', '-R', f'{remote}:{base_dir}'], capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]

# === TV 处理逻辑 ===
def extract_episode_index(filename):
    match = re.match(r'^(\d{1,4})\.', filename)
    return int(match.group(1)) if match else None

def extract_index_title(filename):
    match = re.match(r'^(\d{1,4})_(.+)\.([^.]+)$', filename)
    if match:
        return int(match.group(1)), match.group(2).strip(), match.group(3)
    return None, None, None

def extract_chinese_numbered_title(filename):
    match = re.match(r'^(\d{1,4})\s+(.+)\.([^.]+)$', filename) or \
            re.match(r'^第(\d{1,4})话\s*(.+)\.([^.]+)$', filename)
    if match:
        return int(match.group(1)), match.group(2).strip(), match.group(3)
    match = re.match(r'^第(\d{1,4})话\.([^.]+)$', filename)
    if match:
        return int(match.group(1)), '', match.group(2)
    return None, None, None

def extract_general_numbered_title(filename):
    match = re.match(r'^(\d{1,4})[\s\-_.]*(.*)\.([^.]+)$', filename)
    if match:
        raw_title = re.sub(r'第\d{1,4}话', '', match.group(2)).strip(" -_.")
        return int(match.group(1)), raw_title, match.group(3)
    return None, None, None

def process_tv_file_list_in_dir(remote, base_dir, file_list):
    for f in file_list:
        if not f.lower().endswith(VIDEO_EXTS):
            continue
        path_parts = f.split('/')
        if len(path_parts) < 2:
            continue
        if len(path_parts) == 3:
            middle = path_parts[-2]
            if not re.match(r'(?i)^(season\s*\d+|s\d{1,3})$', middle.strip()):
                log(f"[{remote}] 跳过非 Season/Sx 的三层路径: {f}")
                continue
        elif len(path_parts) > 3:
            log(f"[{remote}] 跳过超过三层的路径: {f}")
            continue

        filename = path_parts[-1]
        old_path = f"{remote}:{base_dir}/{f}"

        if len(path_parts) >= 3:
            parent_dir = path_parts[-2]
            grandparent_dir = path_parts[-3]
            match_season = re.match(r'(?i)(?:season\s*|s)(\d{1,3})', parent_dir.strip())
            season_number = int(match_season.group(1)) if match_season else 1
            folder = grandparent_dir
        else:
            folder = path_parts[-2]
            season_number = 1

        folder = extract_tv_title_from_brackets(folder)   # 精准提取 SUMMER NUDE
        episode, title, ext = None, '', ''
        # ✅ 优先匹配 SxxEyy 结构
        match_sxe = re.search(r'[sS](\d{1,3})[eE](\d{1,4})', filename)
        if match_sxe:
            season_number = int(match_sxe.group(1))
            episode = int(match_sxe.group(2))
            title = filename[match_sxe.end():].rsplit('.', 1)[0].lstrip('. ')
            ext = filename.rsplit('.', 1)[-1]

        else:
            # fallback 匹配仅 episode
            match_e = re.search(r'(?i)e[p]?(\d{1,4})', filename)
            if match_e:
                episode = int(match_e.group(1))
                title = filename[match_e.end():].rsplit('.', 1)[0].lstrip('. ')
                ext = filename.rsplit('.', 1)[-1]
            else:
                episode = extract_episode_index(filename)
                if episode:
                    ext = filename.rsplit('.', 1)[-1]
                else:
                    episode, title, ext = extract_index_title(filename)
                    if episode is None:
                        episode, title, ext = extract_chinese_numbered_title(filename)
                    if episode is None:
                        episode, title, ext = extract_general_numbered_title(filename)

        if episode is None:
            log(f"[{remote}] 跳过未识别集数: {f}")
            continue

        sxe = f"S{season_number:02d}E{episode:04d}"
        new_filename = f"{folder}.{sxe}.{title}.{ext}" if title else f"{folder}.{sxe}.{ext}"
        new_path = f"{remote}:{base_dir}/{'/'.join(path_parts[:-1])}/{new_filename}"

        if old_path == new_path:
            log(f"[{remote}] 跳过：源文件与目标文件相同 → {f}")
            continue

        log(f"[{remote}] 重命名: {old_path} -> {new_path}")
        cmd = ['rclone', 'moveto', old_path, new_path, '--disable', 'DirMove', '--no-traverse']
        if DRY_RUN:
            log('[模拟] ' + ' '.join(cmd))
        else:
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                log(f"[ERROR] rclone moveto 失败")
                log(f"[FROM] {old_path}")
                log(f"[TO  ] {new_path}")
                log(f"[stderr] {e.stderr.strip()}")

# === Movie 专属逻辑（含主标题宽松前缀匹配）===
def extract_title_from_brackets(folder_name):
    match = re.search(r'[\[\(【{（《](.*?)[\]）】】》)]', folder_name)
    return match.group(1).strip() if match else None

def extract_tv_title_from_brackets(folder_name):
    # 1. 优先提取第一个中括号/花括号/中文括号内的内容（如有）
    match = re.search(r'^[\[\(【{（《](.*?)[\]）】】》)]', folder_name)
    if match:
        return match.group(1).strip()

    # 2. 如果是中文开头，只取空格前内容
    if re.match(r'^[\u4e00-\u9fa5]', folder_name):
        return re.sub(r'[（(【{\\[].*?[）)】}\\]]', '', folder_name.split(' ')[0].strip()).strip()

    # 3. 否则返回完整英文目录名（含空格）
    return folder_name.strip()

def extract_main_title(folder_name):
    return re.split(r'[ （(_\-]', folder_name.strip())[0]

def is_prefixed_by(name, title):
    name_words = re.split(r'[\s.\-_]+', name.lower())
    title_words = re.split(r'[\s.\-_]+', title.lower())
    return name_words[:len(title_words)] == title_words

def is_loose_prefix(name, title):
    name_clean = re.sub(r'[.\-_\s]+', '', name.lower())
    title_clean = re.sub(r'[.\-_\s]+', '', title.lower())
    return name_clean.startswith(title_clean)

def process_movie_file_list_in_dir(remote, base_dir, file_list):
    for f in file_list:
        if not f.lower().endswith(VIDEO_EXTS):
            continue

        path_parts = f.split('/')
        if len(path_parts) == 1:
            log(f"[{remote}] 跳过根目录文件: {f}")
            continue
        if len(path_parts) > 2:
            log(f"[{remote}] 跳过超过两层的路径: {f}")
            continue

        folder_name = path_parts[0]
        filename = path_parts[-1]
        old_path = f"{remote}:{base_dir}/{f}"

        name, ext = os.path.splitext(filename)
        ext = ext.lstrip('.')

        if name == folder_name:
            log(f"[{remote}] 跳过：文件名与目录名完全一致 → {f}")
            continue

        if is_prefixed_by(name, folder_name):
            log(f"[{remote}] 跳过：文件名以目录名为前缀 → {f}")
            continue

        title_candidate = extract_title_from_brackets(folder_name)
        if title_candidate and is_prefixed_by(name, title_candidate):
            log(f"[{remote}] 跳过：文件名以提取片名为前缀 → {f}")
            continue

        main_title = extract_main_title(folder_name)
        if main_title and is_loose_prefix(name, main_title):
            log(f"[{remote}] 跳过：文件名以主标题为前缀（宽松匹配） → {f}")
            continue

        new_filename = f"{folder_name}.{ext}"
        new_path = f"{remote}:{base_dir}/{folder_name}/{new_filename}"
        if old_path == new_path:
            log(f"[{remote}] 跳过：源文件与目标文件相同 → {f}")
            continue

        log(f"[{remote}] 重命名电影: {old_path} -> {new_path}")
        cmd = ['rclone', 'moveto', old_path, new_path, '--disable', 'DirMove', '--no-traverse']
        if DRY_RUN:
            log('[模拟] ' + ' '.join(cmd))
        else:
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                log(f"[ERROR] rclone moveto 失败")
                log(f"[FROM] {old_path}")
                log(f"[TO  ] {new_path}")
                log(f"[stderr] {e.stderr.strip()}")

# === 调度器 ===
def process_files():
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for remote, media_types in REMOTES.items():
            for media_type, base_dirs in media_types.items():
                for base_dir in base_dirs:
                    if not remote_path_exists(remote, base_dir):
                        log(f"[WARN] 跳过不存在的路径: {remote}:{base_dir}")
                        continue

                    all_files = list_files(remote, base_dir)
                    dir_groups = defaultdict(list)
                    for f in all_files:
                        parent_dir = '/'.join(f.split('/')[:-1])
                        dir_groups[parent_dir].append(f)

                    for group in dir_groups.values():
                        if media_type == 'tv':
                            futures.append(executor.submit(process_tv_file_list_in_dir, remote, base_dir, group))
                        elif media_type == 'movie':
                            futures.append(executor.submit(process_movie_file_list_in_dir, remote, base_dir, group))
        for future in as_completed(futures):
            future.result()

    log("\n✔ 所有任务处理完成。脚本执行完毕。\n")

# === 启动入口 ===
if __name__ == "__main__":
    process_files()

# ================================
# ✔ DONE: 视频重命名脚本已完成配置
# 支持 TV / Movie 重命名，稳定运行
# 支持 DRY_RUN、并发处理、.nfo 格式
# ================================
