"""PyInstaller 동결 진입점 — 사이드카 FastAPI 서버를 기동한다.

개발용 콘솔 스크립트(meetscribe-sidecar = meetscribe.api:main)와 동일하게
meetscribe.api.main() 을 호출하지만, 동결(frozen) 환경에서 안전하도록
multiprocessing.freeze_support() 를 먼저 부르고, torch 온디맨드 캐시를
sys.path 에 주입한다.

배경:
- PyInstaller 로 동결한 exe 안에서 multiprocessing 이 자식 프로세스를 만들 때
  freeze_support() 가 없으면 자식이 부모(=이 exe)를 처음부터 재실행해 서버가
  무한 재기동된다. 동결 진입점의 필수 가드.
- torch/torchaudio 는 인스톨러 축소를 위해 동결본에서 제외됐다. 첫 실행 시
  ~/.meetscribe/runtime 에 받아두며, runtime_torch.ensure_on_path() 가 그 캐시를
  모든 import 보다 먼저 sys.path 앞에 넣어 torch import 를 가능하게 한다.
  (torch 가 아직 없어도 서버는 뜬다 — /runtime 엔드포인트가 설치를 유도.)
- api.main() → run() 은 uvicorn.run(app 객체, 127.0.0.1, 8765) 로 기동하며
  import-string/reload/factory 를 쓰지 않아 동결 환경에 안전하다.
- 포트는 MEETSCRIBE_PORT / VITE_SIDECAR_PORT 환경변수로 재정의 가능
  (Tauri 셸이 사이드카 spawn 시 주입한다).
"""

import multiprocessing

from meetscribe import runtime_torch

if __name__ == "__main__":
    multiprocessing.freeze_support()  # 동결 자식 프로세스 무한 재기동 방지(필수)
    runtime_torch.ensure_on_path()  # torch 캐시가 있으면 sys.path 주입(모든 import 전)
    from meetscribe.api import main

    raise SystemExit(main())
