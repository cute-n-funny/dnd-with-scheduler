"""Focused regression checks for four-hour default and 4–8-hour configuration.

No Discord connection or token needed.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT=Path(__file__).resolve().parents[1]


def function_in(path, name, class_name=None):
    module=ast.parse((ROOT/path).read_text(encoding='utf-8'))
    nodes=module.body
    if class_name:
        nodes=next(n.body for n in nodes if isinstance(n,ast.ClassDef) and n.name==class_name)
    return next(n for n in nodes if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)


class HoursDefaultTests(unittest.TestCase):
    def test_config_4_to_8_only(self):
        fn=function_in('bot.py','validate_config')
        ns={'ZoneInfo':ZoneInfo,'ZoneInfoNotFoundError':ZoneInfoNotFoundError}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'bot.py','exec'),ns)
        c=dict(weeks=2,poll_duration=2,poll_after_event=3,reminder_before_event=6,
               attendee_limit=8,event_duration=4,hours=4,min_votes=3,
               title='D&D',description='Test',timezone='America/Chicago')
        for h in range(4,9):
            c['hours']=h
            ns['validate_config'](c)
        for h in [1,2,3,9]:
            c['hours']=h
            with self.assertRaises(ValueError):ns['validate_config'](c)

    def test_dropdown_starts_at_four_even_for_legacy_maximum_three(self):
        cls='AvailabilityVotePanel'
        init=function_in('availability_ui.py','__init__',cls)
        assignments={n.targets[0].attr:n.value for n in ast.walk(init)
                     if isinstance(n,ast.Assign) and len(n.targets)==1 and
                     isinstance(n.targets[0],ast.Attribute) and
                     n.targets[0].attr in ('max_hours','selected_hours')}
        self.assertEqual(ast.literal_eval(assignments['selected_hours']),4)
        for maximum in [3,4,8]:
            context={'self':SimpleNamespace(config={'hours':maximum})}
            m=eval(compile(ast.Expression(body=assignments['max_hours']),'ui','eval'),context)
            self.assertEqual(m,max(4,maximum))

    def test_four_hour_label_and_default_only(self):
        node=function_in('availability_ui.py','hour_options','AvailabilityVotePanel')
        class Option:
            def __init__(self,**kw):self.__dict__.update(kw)
        ns={'discord':SimpleNamespace(SelectOption=Option)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'availability_ui.py','exec'),ns)
        for maximum in [4,8]:
            options=ns['hour_options'](SimpleNamespace(max_hours=maximum,selected_hours=4))
            self.assertEqual([o.value for o in options],[str(n) for n in range(1,maximum+1)])
            self.assertEqual([o.value for o in options if o.default],['4'])
            self.assertEqual([o.label for o in options][:4],
                             ['1 hour','2 hours','3 hours','Default (4 hours)'])

    def test_old_schedule_reset_and_test_are_upgraded(self):
        for name in ('reset_scheduler','test_scheduler'):
            node=function_in('bot.py',name)
            hours_updates=[n for n in ast.walk(node) if isinstance(n,ast.Assign) and
                any(isinstance(t,ast.Subscript) and isinstance(t.value,ast.Name)
                    and t.value.id=='c' and isinstance(t.slice,ast.Constant) and
                    t.slice.value=='hours' for t in n.targets)]
            self.assertTrue(hours_updates, f'{name} needs legacy hours normalization')

if __name__=='__main__':unittest.main()
