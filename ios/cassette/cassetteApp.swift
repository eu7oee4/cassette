import SwiftUI

@main
struct cassetteApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .tint(Color.theme)   // 全 app 默认按钮跟主题色走（换主题只改 Color.theme）
        }
    }
}
