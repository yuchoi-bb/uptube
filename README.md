# UpTube

유튜브 링크나 검색어를 입력하면 영상 목록을 보여주고, 원하는 영상의 음원을
MP3로 다운로드하는 웹앱입니다. 파일명은 해당 유튜브 영상의 제목이 됩니다.

## 요구 사항

- Python 3.10+
- ffmpeg (mp3 변환에 필요)
- tmux (서버 운영용)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg tmux

# Python 패키지
pip install -r requirements.txt
```

## 실행 (tmux)

```bash
./run.sh          # 시작 (기본 포트 8000)
./run.sh stop     # 중지
./run.sh restart  # 재시작
./run.sh status   # 상태 확인
./run.sh logs     # 로그 확인
PORT=9000 ./run.sh  # 다른 포트로 실행
```

브라우저에서 `http://<서버주소>:8000` 접속 후:

1. 유튜브 링크(영상/재생목록) 또는 검색어를 입력
2. **▶ 미리듣기**로 음원을 확인
3. **MP3 다운로드** 클릭 → 서버가 추출·변환해 `영상제목.mp3` 로 저장

### 저장 위치

서버가 직접 파일을 저장합니다. 기본 위치는 자동 감지:

| 환경 | 저장 폴더 |
|---|---|
| Termux (termux-setup-storage 완료) | `/storage/emulated/0/Download/uptube` |
| 일반 리눅스/맥 | `~/Downloads/uptube` |
| 그 외 | 프로젝트 내 `downloads/` |

다른 위치를 쓰려면: `UPTUBE_DOWNLOAD_DIR=/원하는/경로 ./run.sh`
같은 제목이 이미 있으면 `제목 (1).mp3` 처럼 번호가 붙습니다.

## Termux(안드로이드)에서 실행

```bash
# 1. 필수 패키지 설치
pkg update && pkg upgrade
pkg install python ffmpeg git tmux

# 2. 저장소(Download 폴더) 접근 권한 부여 - 최초 1회, 팝업에서 허용 선택
termux-setup-storage

# 3. 코드 받기
git clone https://github.com/yuchoi-bb/uptube.git
cd uptube
pip install -r requirements.txt

# 4. 서버 시작 (tmux 세션으로 백그라운드 실행)
./run.sh
```

폰이 잠들면 서버가 멈출 수 있으니 백그라운드 유지 설정을 권장합니다:

```bash
termux-wake-lock   # CPU 슬립 방지
```

안드로이드 설정 → 배터리 → Termux → **배터리 사용량 최적화 제외**도 함께 설정하세요.

### 웹 접근 방법

| 접속 위치 | 주소 |
|---|---|
| Termux를 실행 중인 폰 브라우저 | `http://localhost:8000` |
| 같은 Wi-Fi의 다른 기기(PC 등) | `http://<폰 IP>:8000` |

폰 IP 확인 (Termux에서):

```bash
ip -4 addr show wlan0 | grep inet
```

예: `inet 192.168.0.23/24` 로 나오면 PC 브라우저에서 `http://192.168.0.23:8000` 접속.

서버 관리:

```bash
./run.sh status   # 상태 확인
./run.sh logs     # 로그 보기
./run.sh restart  # 재시작
./run.sh stop     # 중지
tmux attach -t uptube   # 세션 직접 접속 (빠져나오기: Ctrl+b 후 d)
```

## 구조

- `app.py` — Flask 서버. `/api/search` (검색/링크 조회), `/api/download/<id>` (mp3 추출)
- `templates/index.html` — 단일 페이지 UI
- `run.sh` — tmux 세션 관리 스크립트

## 참고

- 음원 추출은 [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg 사용 (192kbps mp3)
- 재생목록 링크를 넣으면 앞쪽 30개 항목까지 표시됩니다
- 저작권이 있는 콘텐츠는 개인 소장 등 허용된 범위 내에서만 사용하세요
