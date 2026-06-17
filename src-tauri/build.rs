// Tauri 2 빌드 스크립트.
//
// tauri-build이 컴파일 타임에 다음을 수행한다:
//  - tauri.conf.json 파싱·검증
//  - capabilities/*.json(권한)으로부터 ACL 코드 생성
//  - Windows 리소스(아이콘 등) 임베드
//
// 표준 형태이며, 별도 커스텀 빌드 로직은 두지 않는다.
fn main() {
    tauri_build::build();
}
