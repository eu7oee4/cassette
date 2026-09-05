import SwiftUI

/// 底部输入栏：➕(附件) + 笑脸(表情包)按钮 + 文字输入框 + 发送按钮。
/// 生成中不禁用（09-05）：想说就说，消息进 app 侧 outbox 排队串行发——
/// 「正在生成」的反馈归 ThinkingNoteRow 三个点小字，不归这排按钮。
struct InputBar: View {
    @Binding var text: String
    var stickersActive: Bool = false  // 表情面板是否已展开，用来高亮笑脸按钮
    var hasAttachments: Bool = false  // 有暂存待发的表情/图片时，即使没文字也能发送
    var onAttach: () -> Void = { }    // ➕：选照片（以后加文件）
    var onStickers: () -> Void = { }  // 展开/收起表情包面板
    let onSend: () -> Void

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || hasAttachments
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 10) {
            // ➕：附件（照片；文件入口以后挂进来）
            Button(action: onAttach) {
                Image(systemName: "plus.circle")
                    .font(.system(size: 22, weight: .regular))
                    .foregroundStyle(Color.userAccent)
                    .frame(width: 32, height: 32)
            }

            // 笑脸按钮：展开表情包面板
            Button(action: onStickers) {
                Image(systemName: stickersActive ? "face.smiling.inverse" : "face.smiling")
                    .font(.system(size: 22, weight: .regular))
                    .foregroundStyle(Color.userAccent)
                    .frame(width: 32, height: 32)
            }

            // 文字输入框："隐形镜像定高 + TextEditor"——TextField(axis:.vertical)+lineLimit(1...5)
            // 对超长无换行文本的内部测量有 bug（滑不到底、光标错位，实机复现）。
            // 镜像 Text 同字体同边距决定容器高度（1~5 行自动生长），TextEditor 铺满其上；
            // 超 5 行后由 UITextView 机件内部滚动，光标定位/滚到光标都是成熟机件。
            // 镜像文本尾部若是换行要垫个空格：SwiftUI Text 会吞尾部空行，
            // 以回车结尾时镜像少算一行高度 → 光标行被裁在容器外（实机症状）。
            Text(text.isEmpty ? "发消息…" : (text.hasSuffix("\n") ? text + " " : text))
                .font(.body)
                .foregroundStyle(text.isEmpty ? Color(.placeholderText) : .clear)
                .lineLimit(1...5)                 // 镜像最多长到 5 行 → 容器高度封顶
                .padding(.horizontal, 5)          // 对齐 UITextView 的内建边距
                .padding(.vertical, 8)            // (lineFragmentPadding 5 / inset 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(
                    TextEditor(text: $text)
                        .font(.body)
                        .scrollContentBackground(.hidden)
                )
                .padding(.horizontal, 9)          // 5+9=14，视觉边距和惯例一致
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)   // 圆角全局对准气泡（§6）
                        .fill(Color(.systemGray6))
                )

            // 发送按钮：只看有没有内容，不看生成状态
            Button(action: onSend) {
                Image(systemName: "chevron.up.circle.fill")
                    .font(.system(size: 30))
                    .foregroundStyle(canSend ? Color.userAccent : Color(.systemGray3))
            }
            .disabled(!canSend)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

/// 待发文件预览条：文件名胶囊行，点 ✕ 移除单个。
struct PendingFilesBar: View {
    let files: [OutgoingFile]
    let onRemove: (Int) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(files.enumerated()), id: \.offset) { idx, f in
                    HStack(spacing: 6) {
                        Image(systemName: "doc.fill")
                            .font(.system(size: 14))
                            .foregroundStyle(Color.userAccent)
                        Text(f.name).font(.footnote).lineLimit(1)
                        Button { onRemove(idx) } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 14))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(Color(.systemGray6), in: Capsule())
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .background(.bar)
    }
}

/// 待发照片预览条：浮在输入框上方的缩略图行，点 ✕ 移除单张。
struct PendingImagesBar: View {
    let images: [Data]
    let onRemove: (Int) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(images.enumerated()), id: \.offset) { idx, data in
                    ZStack(alignment: .topTrailing) {
                        if let img = UIImage(data: data) {
                            Image(uiImage: img)
                                .resizable()
                                .scaledToFill()
                                .frame(width: 56, height: 56)
                                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        }
                        Button { onRemove(idx) } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 16))
                                .foregroundStyle(.white, .black.opacity(0.55))
                        }
                        .offset(x: 5, y: -5)
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .background(.bar)
    }
}
