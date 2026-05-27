from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    firebase_credentials_path: str = "./serviceAccountKey.json"
    firebase_credentials_json: str = ""
    firebase_credentials_b64: str = ""
    firebase_storage_bucket: str = ""
    zoom_sdk_key: str = ""
    zoom_sdk_secret: str = ""
    # Comma-separated list of allowed origins. Single origin works too.
    frontend_origin: str = "http://localhost:5173"

    # WhatsApp Cloud API (Meta). Leave blank to disable WhatsApp dispatch;
    # the app still records notifications in Firestore.
    wa_token: str = ""
    wa_phone_id: str = ""
    wa_tpl_appointment: str = "appointment_scheduled"
    wa_tpl_decision: str = "triage_decision"
    wa_tpl_ruleout: str = "triage_decision"
    wa_lang: str = "en"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
