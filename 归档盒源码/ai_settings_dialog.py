"""AI 接口配置：粘贴密钥或脚本，自动识别常见 OpenAI 兼容厂商。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
)

from operation_feedback import install
from fit_combo import FitComboBox as QComboBox
from ai_fallback import (
    AiSettings,
    apply_provider_defaults,
    detect_from_paste,
    get_provider,
    normalize_base_url,
    provider_choices,
    save_settings,
)


class AiSettingsDialog(QDialog):
    def __init__(self, settings: AiSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("配置 AI 接口")
        self.setMinimumWidth(560)
        self._settings = AiSettings(
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
            enabled=settings.enabled,
            provider_id=settings.provider_id,
            setup_dismissed=settings.setup_dismissed,
        )
        self._build_ui()
        self._load_into_form()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        intro = QLabel(
            "归档盒不内置任何密钥。把你自己的 API Key，或厂商文档里的 "
            "curl / 脚本片段粘贴进来，程序会识别是哪家接口，并填好地址与模型。"
            "只有你勾选「待定文件问 AI」后，才会在整理预览时发请求。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#334155;")
        root.addWidget(intro)

        paste_row = QHBoxLayout()
        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText(
            "粘贴 API Key，或整段 curl / 环境变量脚本……\n"
            "例如：sk-...  或  https://api.deepseek.com/v1 + Authorization: Bearer sk-...")
        self.paste_edit.setFixedHeight(88)
        paste_row.addWidget(self.paste_edit, stretch=1)
        self.btn_detect = QPushButton("识别并填入")
        self.btn_detect.setProperty("role", "primary")
        self.btn_detect.clicked.connect(self._on_detect)
        paste_row.addWidget(self.btn_detect, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(paste_row)

        self.detect_label = QLabel("")
        self.detect_label.setWordWrap(True)
        self.detect_label.setStyleSheet("color:#1D4ED8;font-size:12px;")
        root.addWidget(self.detect_label)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.provider_combo = QComboBox()
        for provider_id, name in provider_choices():
            self.provider_combo.addItem(name, provider_id)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("厂商", self.provider_combo)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow("接口地址", self.base_url_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("模型名")
        form.addRow("模型", self.model_edit)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("你自己的 API Key")
        form.addRow("API Key", self.key_edit)

        self.enabled_check = QCheckBox("启用「待定或识别失败时问 AI」")
        form.addRow("", self.enabled_check)
        root.addLayout(form)

        tip = QLabel(
            "本地「AI 管家」仍可离线使用；这里的密钥只用于联网补充分类。"
            "密钥保存在本机应用数据目录，权限尽量收紧，不会上传到归档盒服务器。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#64748B;font-size:12px;")
        root.addWidget(tip)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.feedback = install(root, self.detect_label, hide_source=False)

    def _load_into_form(self) -> None:
        idx = self.provider_combo.findData(self._settings.provider_id)
        if idx < 0:
            idx = self.provider_combo.findData("custom")
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(0, idx))
        self.provider_combo.blockSignals(False)
        self.base_url_edit.setText(self._settings.base_url)
        self.model_edit.setText(self._settings.model)
        self.key_edit.setText(self._settings.api_key)
        self.enabled_check.setChecked(bool(self._settings.enabled))

    def _on_provider_changed(self, *_args) -> None:
        provider_id = str(self.provider_combo.currentData() or "custom")
        if provider_id == "custom":
            return
        spec = get_provider(provider_id)
        self.base_url_edit.setText(spec.base_url)
        self.model_edit.setText(spec.model)

    def _on_detect(self) -> None:
        text = self.paste_edit.toPlainText().strip()
        if not text:
            text = self.key_edit.text().strip() or self.base_url_edit.text().strip()
        detected = detect_from_paste(text)
        idx = self.provider_combo.findData(detected.provider_id)
        if idx < 0:
            idx = self.provider_combo.findData("custom")
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(0, idx))
        self.provider_combo.blockSignals(False)
        self.base_url_edit.setText(detected.base_url)
        self.model_edit.setText(detected.model)
        if detected.api_key:
            self.key_edit.setText(detected.api_key)
        self.detect_label.setText(
            f"识别结果：{detected.provider_name}。"
            + (" ".join(detected.notes) if detected.notes else ""))

    def _on_save(self) -> None:
        provider_id = str(self.provider_combo.currentData() or "custom")
        base_url = normalize_base_url(self.base_url_edit.text())
        model = self.model_edit.text().strip()
        api_key = self.key_edit.text().strip()
        enabled = bool(self.enabled_check.isChecked())
        if enabled and not api_key:
            self.feedback.reflect("要启用联网 AI，请先填写 API Key。")
            QMessageBox.warning(self, "配置 AI 接口", "要启用联网 AI，请先填写 API Key。")
            return
        if enabled and not base_url:
            self.feedback.reflect("要启用联网 AI，请先填写接口地址。")
            QMessageBox.warning(self, "配置 AI 接口", "要启用联网 AI，请先填写接口地址。")
            return
        if provider_id != "custom" and (not base_url or not model):
            apply_provider_defaults(self._settings, provider_id)
            base_url = base_url or self._settings.base_url
            model = model or self._settings.model
        self._settings.provider_id = provider_id
        self._settings.base_url = base_url
        self._settings.model = model
        self._settings.api_key = api_key
        self._settings.enabled = bool(enabled and api_key and base_url)
        self._settings.setup_dismissed = True
        try:
            save_settings(self._settings)
        except OSError as exc:
            self.feedback.reflect(f"设置没能保存到磁盘：{exc}")
            QMessageBox.warning(
                self, "配置 AI 接口",
                f"设置没能保存到磁盘：\n{exc}")
            return
        self.accept()

    def result_settings(self) -> AiSettings:
        return self._settings
