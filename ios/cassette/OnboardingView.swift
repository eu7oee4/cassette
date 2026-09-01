import SwiftUI
import PhotosUI

/// 首次启动的起名引导：给 AI 和自己各起个名字、选个头像（都可跳过），之后设置页/点头像随时改。
struct OnboardingView: View {
    @ObservedObject var store: ProactiveSettingsStore
    @ObservedObject var profile: ProfileStore
    /// 这次引导在给**谁**起名选头像。首启时就是默认角色，但还是显式传——
    /// setAvatar 过一次 loadTransferable 才落地，落地时再问「现在是谁」正是那个坑。
    let charID: String
    let onDone: () -> Void

    @State private var agentDraft = ""
    @State private var userDraft = ""
    @State private var agentPickerItem: PhotosPickerItem?
    @State private var userPickerItem: PhotosPickerItem?

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            Image(systemName: "recordingtape")
                .font(.system(size: 48, weight: .medium))
                .foregroundStyle(Color.userAccent)
            Text("先起个名字")
                .font(.title2.bold())

            // 头像（可选）：左 TA、右自己。不选就用默认占位。
            HStack(spacing: 48) {
                avatarPicker(sender: .other, label: "TA 的头像", selection: $agentPickerItem)
                avatarPicker(sender: .me, label: "你的头像", selection: $userPickerItem)
            }

            VStack(spacing: 14) {
                nameField("TA 的昵称", text: $agentDraft, placeholder: "cassette")
                nameField("你的昵称", text: $userDraft, placeholder: "user")
            }
            .padding(.horizontal, 32)

            Text("头像和昵称之后都能修改。")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)

            Button(action: finish) {
                Text("开始")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .padding(.horizontal, 32)
            Spacer()
            Spacer()
        }
        .background(Color(.systemBackground))
        .onChange(of: agentPickerItem) { _, item in loadAvatar(item, for: .other) }
        .onChange(of: userPickerItem) { _, item in loadAvatar(item, for: .me) }
    }

    private func avatarPicker(sender: MessageSender, label: String,
                              selection: Binding<PhotosPickerItem?>) -> some View {
        PhotosPicker(selection: selection, matching: .images) {
            VStack(spacing: 8) {
                Group {
                    if let img = profile.avatar(for: sender, char: charID) {
                        Image(uiImage: img).resizable().scaledToFill()
                    } else {
                        Color(.systemGray5)
                            .overlay(
                                Image(systemName: "plus")
                                    .font(.system(size: 22, weight: .medium))
                                    .foregroundStyle(Color.userAccent)
                            )
                    }
                }
                .frame(width: 68, height: 68)
                .clipShape(Circle())
                Text(label)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
    }

    private func loadAvatar(_ item: PhotosPickerItem?, for sender: MessageSender) {
        guard let item else { return }
        let char = charID          // 发起那一刻钉死，别等 await 落地再问「现在是谁」
        Task {
            if let data = try? await item.loadTransferable(type: Data.self),
               let img = UIImage(data: data) {
                profile.setAvatar(sender, image: img, char: char)
            }
        }
    }

    private func nameField(_ label: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.footnote)
                .foregroundStyle(.secondary)
            TextField(placeholder, text: text)
                .textFieldStyle(.roundedBorder)
        }
    }

    private func finish() {
        let a = agentDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let u = userDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        store.settings.agentName = a.isEmpty ? "cassette" : String(a.prefix(20))
        store.settings.userName = u.isEmpty ? "user" : String(u.prefix(20))
        Task { await store.pushToServer() }
        onDone()
    }
}
