import SwiftUI

/// 输入栏上方的「待发表情」条：缩略图 + 右上角 ✕ 取消。按发送时随文字一起发。
struct PendingStickerBar: View {
    let imageURL: URL
    let onRemove: () -> Void

    var body: some View {
        HStack {
            ZStack(alignment: .topTrailing) {
                Group {
                    if let img = UIImage(contentsOfFile: imageURL.path) {
                        Image(uiImage: img).resizable().scaledToFill()
                    } else {
                        Color(.systemGray5)
                    }
                }
                .frame(width: 60, height: 60)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                Button(action: onRemove) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(.white, .black.opacity(0.5))
                }
                .padding(3)
            }
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }
}
