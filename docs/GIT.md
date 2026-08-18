# Git · 배포 명령 모음 (arena)

이 저장소의 커밋·푸시는 **직접** 실행한다. 아래는 복붙용 명령 모음이다.
(에이전트 이미지 push 는 git 과 무관 — 맨 아래 "에이전트 배포" 참고)

## 0. 최초 1회 — 원격 연결 + 첫 push

이미 로컬에 첫 커밋이 있다(`git log` 로 확인). 원격만 붙여 push 하면 된다.

```bash
cd dah-arena
git remote add origin git@github.com:DAH-aexploit/arena.git
git branch -M main

# 이후 새로 추가된 파일(docs/GIT.md 등)까지 첫 커밋에 넣으려면:
git add -A
git commit --amend --no-edit        # 첫 커밋에 합치기 (메시지 유지)
#   ↑ 메시지를 새로 쓰려면 --no-edit 빼고 에디터에서 작성

git push -u origin main
```

> 첫 커밋을 직접 새로 작성하고 싶으면(작성자·메시지 모두 내 손으로):
> ```bash
> git reset --soft HEAD~1     # 커밋만 취소, 변경은 staged 로 유지
> git add -A
> git commit                  # 에디터에서 직접 작성
> git push -u origin main
> ```

## 1. 평소 커밋

```bash
git add -A                    # 또는 원하는 파일만
git status --short            # .env·docker-compose.yml·data/·pcap 이 없어야 정상
git commit                    # 에디터에서 메시지 작성
git push
```

`.gitignore` 가 `.env`, `docker-compose.yml`(LLM 키 박힘), `data/`, `*.pcap` 을
제외한다. `git status` 에 이들이 보이면 커밋하지 말 것.

## 2. 브랜치로 작업(권장)

```bash
git checkout -b feat/<작업이름>
# ... 수정 ...
git add -A && git commit
git push -u origin feat/<작업이름>
# GitHub 에서 PR 생성 (또는: gh pr create)
```

## 3. 팀원 변경 받기

```bash
git pull --rebase origin main
```

---

## 에이전트 배포 (git 아님 — 이미지 push)

방어·공격 에이전트는 소스 커밋이 아니라 **이미지 push** 로 아레나에 반영된다.

```bash
# 로컬 모드(혼자, 같은 PC): make up 이 알아서 빌드·시드
make push-defender TEAM=9 CTX=../defense-agent RELOAD=1
make push-attacker TEAM=9 CTX=../rehearsal-attacker/attacker RELOAD=1

# GHCR 모드(팀 협업): .env 에 REGISTRY_MODE=remote 후, 1회 로그인
make ghcr-login GH_USER=<github-id> GH_PAT=<packages r/w PAT>
make push-defender TEAM=9 CTX=../defense-agent      # ghcr.io/dah-aexploit/team9-defender
make push-attacker TEAM=9 CTX=../rehearsal-attacker/attacker
```

자세한 협업 흐름은 [`COLLAB.md`](COLLAB.md).
