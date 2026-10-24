# 외부 구성 요소와 배포 고지

빌드는 실제 사용한 배포본의 원문 고지를 자동 수집합니다. 원문을 찾을 수 없으면
빌드가 실패합니다. 생성된 `THIRD_PARTY_NOTICES.txt`는 ZIP, 설치 후 플러그인 폴더,
백엔드 EXE 내부, 설치 EXE 내부에 포함됩니다. 이 문서는 원문 고지를 대체하지 않습니다.

| 구성 요소 | 고지 출처 |
| --- | --- |
| 공식 CPython 3.13.17 및 Windows 구성 요소 | Python 배포본의 전체 `LICENSE.txt` |
| Pillow 12.3.0 및 이미지 코덱 | 설치한 wheel의 `pillow-*.dist-info/licenses/LICENSE` 전체 |
| PyInstaller 6.22.0 부트로더·런타임 훅 | `pyinstaller-*.dist-info/licenses/COPYING.txt` 전체 |

Pillow wheel의 고지는 포함된 FreeType·JPEG·PNG·WebP·AVIF 등 관련 라이브러리의
조건도 담고 있습니다. FreeType은 FreeType License 선택지를 사용하며 JPEG/FreeType
표시 문구도 생성 고지에 포함합니다. Python Windows 배포본의 고지는 Microsoft 런타임,
OpenSSL 관련 Apache 라이선스와 기타 포함 구성 요소의 조건을 보존합니다.
PyInstaller 빌드 도구 전체를 배포하는 것이 아니라 실행 파일에 포함되는 부트로더와 훅을 배포합니다.

- https://docs.python.org/3.13/license.html
- https://github.com/python-pillow/Pillow/blob/main/LICENSE
- https://pyinstaller.org/en/stable/license.html

빌드 의존성의 버전이나 wheel이 바뀌면 포함 구성 요소와 고지도 다시 확인해야 합니다.
이 고지는 프로젝트 자체에 MIT 등 새 라이선스를 지정하지 않으며, 내려받는 메타데이터·이미지·자막의
권리를 부여하지 않습니다. 본 프로젝트의 재사용 라이선스는 저장소 소유자가 별도로 정합니다.
