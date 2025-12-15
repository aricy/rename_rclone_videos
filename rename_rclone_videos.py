import subprocess
import re
import os
import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# === 配置项 ===
REMOTES = {
    'quark': {
        'tv': ['quark/tv'],
        'movie': ['quark/movie']
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
MAX_WORKERS = 16

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
    cmd = ['rclone', 'lsf', f'{remote}:{path}']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"[DEBUG] rclone lsf failed for {remote}:{path}")
            log(f"[DEBUG] stdout: {result.stdout.strip()}")
            log(f"[DEBUG] stderr: {result.stderr.strip()}")
        return result.returncode == 0
    except Exception as e:
        log(f"[ERROR] Exception in remote_path_exists: {e}")
        return False

def list_files(remote, base_dir):
    result = subprocess.run(['rclone', 'lsf', '-R', f'{remote}:{base_dir}'], capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]

# === TV 处理逻辑 ===
def extract_episode_info(filename, default_season=1):
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
    stem = filename.rsplit('.', 1)[0]
    
    # 1. SxxEyy or SxxEyy-Ezz
    match = re.search(r'(?i)s(\d{1,3})e(\d{1,4})(?:-?e?(\d{1,4}))?', filename)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        title = filename[match.end():].rsplit('.', 1)[0].strip('. -_')
        return season, episode, title, ext
        
    # 2. 1x01
    match = re.search(r'(\d{1,3})[xX](\d{1,4})', filename)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        title = filename[match.end():].rsplit('.', 1)[0].strip('. -_')
        return season, episode, title, ext

    # 3. Ep.01 / Episode 01
    match = re.search(r'(?i)ep(?:isode)?\.?\s*(\d{1,4})', filename)
    if match:
        season = default_season
        episode = int(match.group(1))
        title = filename[match.end():].rsplit('.', 1)[0].strip('. -_')
        return season, episode, title, ext

    # 4. "01 Title" or "01.Title" (Start of line)
    # Exclude years (19xx, 20xx)
    match = re.match(r'^(\d{1,3})[\s._-]+(.+)$', stem)
    if match:
        val = int(match.group(1))
        if val < 1900: 
            return default_season, val, match.group(2).strip(), ext

    # 5. "第01话" or "第01集"
    match = re.match(r'^第(\d{1,4})[话集]\s*(.*)$', stem)
    if match:
        return default_season, int(match.group(1)), match.group(2).strip(), ext
             
    # 6. Just a number "01"
    if re.match(r'^\d{1,3}$', stem):
        val = int(stem)
        if val < 1900:
             return default_season, val, '', ext

    return None, None, None, None

def clean_episode_title(title):
    if not title: return ''
    # Remove checksums like [A1B2C3D4]
    title = re.sub(r'\[[0-9A-Fa-f]{8}\]', '', title)
    
    # Common technical tags
    # Added H.265 support, 2Audio, etc
    tags_list = [
        r'1080p', r'720p', r'2160p', r'4k', 
        r'x264', r'x265', r'h.?265', r'hevc', r'avc',
        r'aac', r'ac3', r'dts', r'web-dl', r'bluray', r'remux', r'hq',
        r'2audio', r'h.?264', r'dd5.?1', r'uhd'
    ]
    # Join with |
    tags = '|'.join(tags_list)
    
    # 1. Remove [tag] or (tag)
    title = re.sub(r'(?i)[\[\(]\s*(?:' + tags + r')\s*[\]\)]', '', title)
    
    # 2. Remove tags that are surrounded by separators or boundaries
    # Using specific lookarounds or non-capturing groups to ensure we match "dot-tag-dot" or "space-tag-space"
    # To handle "H.265", we need to be careful. The regex "h.?265" covers H.265 or H265.
    
    # Strategy: Replace all separators (., _, -) with spaces temporarily to safely identify words
    # Then remove the words that are technical tags
    # Then collapse spaces and maybe restore dots if needed, but usually we want Clean Title
    
    # Let's try a safer regex approach:
    # Match boundary or separator + tag + boundary or separator
    
    # Iterate to remove all occurrences
    while True:
        # Match tag surrounded by non-word chars or start/end
        # We use a broad separator match [.\-_ ]
        prev_len = len(title)
        title = re.sub(r'(?i)(?:^|[.\-_ ])(?:' + tags + r')(?:$|[.\-_ ])', '.', title)
        # Also handle tags combined like "H.265.AAC" -> we might need multiple passes or better alignment
        
        # Cleanup separators after substitution
        title = re.sub(r'[.\-_ ]{2,}', '.', title)
        
        if len(title) == prev_len:
            break
            
    # 3. Cleanup empty brackets
    title = re.sub(r'\[\s*\]', '', title)
    title = re.sub(r'\(\s*\)', '', title)
    
    # 3.5. Remove redundant "Episode X" info from title content
    # matches: "第 2 集", "Ep 2", "Episode 2"
    title = re.sub(r'(?i)(?:^|[.\-_ ])第\s*\d+\s*[集话部](?:$|[.\-_ ])', '.', title)
    title = re.sub(r'(?i)(?:^|[.\-_ ])(?:ep|episode)\s*\d+(?:$|[.\-_ ])', '.', title)
    
    # 4. Collapse separators again and strip
    title = re.sub(r'[.\-_ ]{2,}', '.', title)
    
    # 5. Final strip
    return title.strip('. -_')

def process_tv_file_list_in_dir(remote, base_dir, file_list):
    for f in file_list:
        if not f.lower().endswith(VIDEO_EXTS):
            continue
        path_parts = f.split('/')
        if len(path_parts) < 2:
            continue
        if len(path_parts) == 3:
            middle = path_parts[-2]
            # Relaxed check: allow "Season X", "Sx", "第 x 季"
            # Or just check if it LOOKS like a season folder
            # If it contains "Season" or starts with "S" digit or "第" digit
            if not re.search(r'(?i)(season|s\d|第\s*\d+\s*[季部])', middle.strip()):
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
        # ✅ 下方统一逻辑
        season_number, episode, title, ext = extract_episode_info(filename, season_number)
        
        if title:
            title = clean_episode_title(title)

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
    # Only if it starts with a bracket (strict mode to avoid cleaning "Title (Year)")
    if re.match(r'^[\[\(【{（《]', folder_name):
        match = re.search(r'^[\[\(【{（《](.*?)[\]）】】》)]', folder_name)
        return match.group(1).strip() if match else None
    return None

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
    # Check if this looks like a collection (multiple video files)
    # If so, we should arguably NOT rename the files to match the folder, 
    # as that would overwrite unique suffixes (like "Kung Fu Panda 1", "Kung Fu Panda 2")
    # with just "Kung Fu Panda Collection.mkv" (or introduce conflict).
    is_collection = len(file_list) > 1
    
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

        clean_name = extract_title_from_brackets(folder_name)
        new_filename = f"{folder_name}.{ext}"
        
        # If we found a cleaner name from brackets (e.g. [Title]... -> Title), use it
        if clean_name:
            # Enforce dot format: "Kung Fu Panda 1-4" -> "Kung.Fu.Panda.1-4"
            clean_name = re.sub(r'\s+', '.', clean_name)
            new_filename = f"{clean_name}.{ext}"
        
        # If no clean name found, continue with checks
        else:
             if name == folder_name:
                 log(f"[{remote}] 跳过：文件名与目录名完全一致 → {f}")
                 continue

             if is_collection:
                 # Collection Mode: Do NOT rename to folder name.
                 # Only strictly clean the filename if needed, or skip.
                 # If we rename "Kung Fu Panda 1.mkv" to "Kung Fu Panda Collection.mkv", it's bad.
                 # So we skip the "Folder Name" rename logic entirely.
                 # We could apply clean_episode_title logic to the filename itself?
                 # For now, let's just log skipping to be safe and avoid destruction.
                 log(f"[{remote}] 跳过合集文件夹内的重命名 (防止覆盖): {f}")
                 continue

             if is_prefixed_by(name, folder_name):
                 log(f"[{remote}] 跳过：文件名以目录名为前缀 → {f}")
                 continue

             title_candidate = extract_title_from_brackets(folder_name) # This matches logic above but logic above is strict now
             # Actually `clean_name` is the strict one.
             # We might still want "is_prefixed_by" logic?
             # If `clean_name` is set, we skip these checks because we WANT to rename to `clean_name`.

        # Special Check: if new filename matches old filename (case insensitive perhaps?)
        # Just trust existing `old_path == new_path` check at end of loop.

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
