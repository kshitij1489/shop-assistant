from django import forms
from chatbot_core.capabilities import CAPABILITIES


class CapabilityTopicForm(forms.Form):
    intent = forms.ChoiceField(label='Capability', choices=[(name, name.replace('_', ' ').title()) for name in CAPABILITIES])
    sub_intent = forms.RegexField(r'^[a-z][a-z0-9_]{0,119}$', label='Topic', max_length=120,
                                 help_text='For cafe information, add any topic such as pet_policy.')
    enabled = forms.BooleanField(required=False, initial=True)
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))
    examples = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), help_text='One example message per line.')
    instructions = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))
    knowledge = forms.JSONField(required=False, widget=forms.Textarea(attrs={'rows': 4}),
                               help_text='JSON text, object, or list containing the facts for this topic.')

    def clean(self):
        data = super().clean()
        capability = CAPABILITIES.get(data.get('intent'))
        if capability and data.get('sub_intent') and not capability.supports(data['sub_intent']):
            self.add_error('sub_intent', 'Choose an implemented topic listed below. New executable actions require backend code.')
        return data
