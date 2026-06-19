# -*- mode: python ; coding: utf-8 -*-
# MeetScribe 사이드카 동결 spec (torch 온디맨드 버전)
# ─────────────────────────────────────────────────────────────
# torch/torchaudio(+CUDA DLL, 약 6.88GB)는 번들에서 제외하고 첫 실행 시
# 온디맨드 다운로드한다(인스톨러 축소: 7.4GB → 약 0.5GB).
# 분석 그래프가 끊기지 않게 hidden-import 로만 남기고, 실제 산출물(binaries/datas/pure)에서는
# torch·torchaudio·CUDA DLL 을 후처리로 필터 제거한다. 단 *.dist-info(메타데이터)는
# importlib.metadata.version('torch') 조회가 깨지지 않게 보존한다.
# ctranslate2(faster-whisper STT 백엔드)는 자체 CUDA DLL 을 ctranslate2\ 안에 두므로
# 아래 정규식(torch 폴더 기반)이 건드리지 않는다 → torch 없이도 STT 는 동작.
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = []
binaries = []
# torch/torchaudio 는 collect_all 하지 않고(본체 제외) 분석용 hidden-import 로만 남긴다.
hiddenimports = ['sklearn.utils._typedefs', 'sklearn.neighbors._partition_nodes',
                 'torch', 'torchaudio']
datas += collect_data_files('sklearn')
datas += copy_metadata('torch')          # version 조회용 메타데이터는 유지(본체는 후처리 제거)
datas += copy_metadata('tqdm')
datas += copy_metadata('regex')
datas += copy_metadata('lightning')
datas += copy_metadata('pytorch_lightning')
datas += copy_metadata('pyannote.audio')
datas += copy_metadata('speechbrain')
datas += copy_metadata('transformers')
datas += copy_metadata('huggingface_hub')
datas += copy_metadata('tokenizers')
datas += copy_metadata('filelock')
datas += copy_metadata('numpy')
datas += copy_metadata('packaging')
hiddenimports += collect_submodules('meetscribe')
# torch/torchaudio 는 의도적으로 collect_all 제외(온디맨드). 나머지는 그대로 수집.
tmp_ret = collect_all('whisperx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('ctranslate2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyannote')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyannote.audio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('speechbrain')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('lightning')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('lightning_fabric')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pytorch_lightning')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('asteroid_filterbanks')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('torchmetrics')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('transformers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('huggingface_hub')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sentencepiece')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['entry.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# ── torch/torchaudio + CUDA DLL 후처리 제거(온디맨드) ──────────────────────────
# 의존 분석으로 딸려온 torch 본체(.pyd/.dll/.py)를 산출물(binaries/datas/pure)에서 뺀다.
# 정규식은 'torch'/'nvidia' 등 다음에 구분자(/ \ . 또는 끝)가 와야 매칭 →
#   - torchmetrics/torchvision 같은 '유지 대상'은 보존(torch 뒤에 글자가 이어짐)
#   - torch\lib\*.dll(cudnn/cublas 등 torch 동봉 CUDA)은 torch\ 경로라 함께 제거
#   - ctranslate2\*.dll(STT용 CUDA)은 ctranslate2\ 경로라 보존
# *.dist-info(버전 조회용)는 항상 보존.
import re as _re
_DROP = _re.compile(
    r'(^|[\\/])(torch|torchaudio|torchgen|functorch|nvidia)([\\/.]|$)',
    _re.I)
def _keep(entry_name):
    if '.dist-info' in entry_name.lower():   # 메타데이터 보존(importlib.metadata.version)
        return True
    return _DROP.search(entry_name) is None
a.binaries = [x for x in a.binaries if _keep(x[0])]
a.datas = [x for x in a.datas if _keep(x[0])]
a.pure = [x for x in a.pure if _keep(x[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='meetscribe-sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='meetscribe-sidecar',
)
