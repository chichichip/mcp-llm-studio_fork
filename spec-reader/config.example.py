# config.py 로 복사해서 사용. config.py 는 .gitignore 에 있음.
# 사내망에서만 채우고, 외부망 저장소에는 절대 올리지 말 것.

URL = "http://<사내주소>/v1/chat/completions"
MODEL = "<모델명>"
HEADERS = {"Content-Type": "application/json"}
TIMEOUT = 300
