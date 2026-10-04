from mongoengine import Document, StringField, DictField

class IntentPrompt(Document):
    api_key = StringField(required=True)
    slug = StringField(required=True)
    intent = StringField(required=True)
    sub_intent = StringField(required=True)
    prompt = StringField(required=True)

    meta = {
        'collection': 'intents',
        'indexes': [
            {'fields': ('api_key', 'sub_intent')}
        ]
    }

class KnowledgeBase(Document):
    api_key = StringField(required=True)
    slug = StringField(required=True)
    intent = StringField(required=True)
    sub_intent = StringField(required=True)
    data = DictField(required=True)

    meta = {
        'collection': 'knowledge_base',
        'indexes': [
            {'fields': ('api_key', 'sub_intent')}
        ]
    }
