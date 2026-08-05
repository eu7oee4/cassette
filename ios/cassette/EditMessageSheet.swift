import SwiftUI

/// 编辑消息的弹窗：改文字。
/// Edit＝只改不重新生成（两边都有）；Regenerate＝改并重新回复（只有自己的消息有）。
struct EditMessageSheet: View {
    @Binding var text: String
    var canRegenerate: Bool = false     // 自己的消息才给「重新生成」
    let onEdit: () -> Void              // 只改，不重新生成
    var onRegenerate: () -> Void = {}   // 改并重新回复
    let onCancel: () -> Void

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                TextEditor(text: $text)
                    .frame(minHeight: 120)
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .fill(Color(.systemGray6))
                    )
                    .padding(.horizontal)
                    .padding(.top)

                Button(action: onEdit) {
                    Text("Edit").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .padding(.horizontal)

                if canRegenerate {
                    Button(action: onRegenerate) {
                        Text("Regenerate").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .padding(.horizontal)
                }

                Spacer()
            }
            .navigationTitle("编辑消息")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", action: onCancel)
                }
            }
        }
        .presentationDetents([.medium])
    }
}
