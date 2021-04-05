import ast
import inspect
import traceback
from datetime import datetime
from io import StringIO
from typing import Any, Callable, Dict, Iterable, Optional, Set

from prompt_toolkit import HTML, print_formatted_text, PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import PygmentsTokens
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from pygments import lex
from pygments.lexers.python import PythonLexer, PythonTracebackLexer

from .polytracker import version
from .plugins import Command, COMMANDS


class PolyTrackerCompleter(Completer):
    def __init__(self, repl: "PolyTrackerREPL"):
        self.repl: PolyTrackerREPL = repl
        self.current_help: Optional[str] = None

    @staticmethod
    def _get_completions(partial: str, options: Iterable[str], already_completed: Set[str], style: str = ""):
        for var in options:
            if var not in already_completed and var.startswith(partial) and var != partial:
                yield Completion(var, start_position=-len(partial), style=style)
                already_completed.add(var)

    def rprompt(self):
        return None

    def bottom_toolbar(self):
        if self.current_help is not None:
            return self.current_help
        else:
            return ""

    def get_completions(self, document, complete_event):
        if self.repl.multi_line and document.text == "":
            # we are in a multi-line statement, and the user pressed TAB in order to indent
            return
        partial = document.text_before_cursor
        already_yielded = set()
        if partial == document.text:
            # we are at the start of the line, so complete for commands:
            yield from PolyTrackerCompleter._get_completions(partial, PolyTrackerREPL.commands, already_yielded,
                                                             "fg:ansiblue")
        args = document.text.split(" ")
        if args[0] in PolyTrackerREPL.commands:
            # We are completing a command
            # TODO: Parse options and add their help to self.current_help
            self.current_help = None
        else:
            self.current_help = None

        yield from PolyTrackerCompleter._get_completions(
            partial, (var for var in self.repl.state if var not in self.repl.builtins), already_yielded
        )
        yield from PolyTrackerCompleter._get_completions(partial, self.repl.builtins, already_yielded, "fg:ansigreen")
        if "__builtins__" in self.repl.state:
            builtins = self.repl.state["__builtins__"]
        else:
            builtins = __builtins__
        yield from PolyTrackerCompleter._get_completions(partial, builtins, already_yielded, "fg:ansigreen")
        if "." in partial:
            portions = partial.split(".")
            varname = portions[-2]
            to_complete = portions[-1]
            if varname in self.repl.state:
                attr = self.repl.state[varname]
                yield from PolyTrackerCompleter._get_completions(
                    to_complete, (a for a in dir(attr) if not a.startswith("_")), already_yielded
                )


class REPLCommand:
    def __init__(self, name: str, func: Callable[..., Any]):
        self._name: str = name
        self._func: Callable[..., Any] = func
        if " " in name or "\t" in name or "\n" in name:
            raise ValueError(f"Command name {name!r} must not contain whitespace")
        elif func.__doc__ is None or func.__doc__ == "":
            raise ValueError(f"Command {name!r}/{func!r} must define a docstring for its help message")
        self._help: str = inspect.getdoc(self.func)
        self.__doc__ = self.help
        try:
            inspect.getcallargs(func)
            return_annotation = inspect.signature(func).return_annotation
            self._can_be_called_with_no_args: bool = (
                    return_annotation == inspect.Signature.empty or return_annotation is None
            )
        except TypeError:
            self._can_be_called_with_no_args = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def func(self) -> Callable[..., Any]:
        return self._func

    @property
    def help(self) -> str:
        return self._help

    def run_bare(self):
        """called when the command is run from the REPL with no parenthesis"""
        if self._can_be_called_with_no_args:
            self.func()
        else:
            print_function_help(self._func, self._name)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


def print_function_help(func, func_name: Optional[str] = None):
    if func_name is None:
        func_name = func.__name__
    sig = inspect.signature(func)
    func_signature = f"{func_name}{sig!s}"
    print_formatted_text(PygmentsTokens(lex(func_signature, lexer=PythonLexer())))
    if func.__doc__ is not None:
        print_formatted_text(HTML(f"<b>{func.__doc__}</b>"))


class PolyTrackerREPL:
    commands: Dict[str, REPLCommand] = {}

    def __init__(self):
        self.session = PromptSession(lexer=PygmentsLexer(PythonLexer))
        self.state = {
            "copyright": f"Copyright (c) 2019-{datetime.today().year} Trail of Bits.\nAll Rights Reserved.",
            "credits": """    PolyTracker was developed by Carson Harmon, Evan Sultanik, and Brad Larsen at Trail of Bits.
    Thanks to Sergey Bratus of DARPA and Galois, Inc. for partially funding this work.
    Also thanks to the LLVM dfsan project for providing a framework off of which to build,
    as well as the Angora project for inspiration.
""",
        }
        self.state.update(self.commands)
        self.builtins = set(self.state.keys())
        self.multi_line: bool = False

    @classmethod
    def register(cls, command_name: str):
        """Function decorator for registering a command with this REPL"""
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if command_name in cls.commands:
                raise ValueError(f"REPL command {command_name!r} is already registered to function "
                                 f"{cls.commands[command_name]!r}")
            cls.commands[command_name] = REPLCommand(name=command_name, func=func)
            return func
        return decorator

    def print_exc(self):
        buffer = StringIO()
        traceback.print_exc(file=buffer)
        tokens = lex(buffer.getvalue(), lexer=PythonTracebackLexer())
        print_formatted_text(PygmentsTokens(tokens))

    def run_python(self, command):
        continued_prompt = HTML("<b>... </b>")

        bindings = KeyBindings()

        @Condition
        def is_multi_line():
            return self.multi_line

        @bindings.add("c-h", filter=is_multi_line, eager=True)
        def _(event):
            buffer = event.app.current_buffer
            doc = buffer.document
            cursor_pos = doc.cursor_position_col
            if cursor_pos > 0 and cursor_pos % 4 == 0 and all(c == " " for c in doc.text[:cursor_pos]):
                # the user pressed BACKSPACE after an indent
                buffer.delete_before_cursor(4)
            else:
                buffer.delete_before_cursor(1)

        @bindings.add("c-i", filter=is_multi_line, eager=True)
        def _(event):
            buffer = event.app.current_buffer
            doc = buffer.document
            cursor_pos = doc.cursor_position_col
            if cursor_pos % 4 == 0 and all(c == " " for c in doc.text[:cursor_pos]):
                # the user pressed TAB intending to indent
                buffer.insert_text("    ")
            elif buffer.complete_state:
                buffer.complete_next()
            else:
                buffer.start_completion(select_first=False)

        self.multi_line = False

        try:
            while True:
                try:
                    parsed = ast.parse(command)
                    is_assignment = any(
                        isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign) or isinstance(node, ast.AugAssign)
                        for node in ast.walk(parsed)
                    )
                    break
                except IndentationError as e:
                    if e.msg.startswith("expected an indented block"):
                        self.multi_line = True
                    else:
                        raise e
                except SyntaxError as e:
                    if e.msg.startswith("unexpected EOF"):
                        self.multi_line = True
                    else:
                        raise e
                if self.multi_line:
                    next_line = self.session.prompt(
                        continued_prompt,
                        complete_while_typing=True,
                        auto_suggest=AutoSuggestFromHistory(),
                        completer=PolyTrackerCompleter(self),
                        key_bindings=bindings
                    )
                    if len(next_line) == 0:
                        is_assignment = True
                        break
                    command = f"{command}\n{next_line}"

            if command in self.commands:
                self.commands[command].run_bare()
            elif is_assignment or self.multi_line:
                exec(command, self.state)
            else:
                result = eval(command, self.state)
                if hasattr(result, "__name__") and result.__name__ == command and command in __builtins__:
                    try:
                        print_function_help(result)
                    except ValueError:
                        print(result)
                else:
                    print(result)
        finally:
            self.multi_line = False

    @classmethod
    def commands_command(cls):
        """print the PolyTracker commands"""
        longest_command = max(len(cmd_name) for cmd_name in cls.commands)
        for name, command in cls.commands.items():
            dots = "." * (longest_command - len(name) + 1)
            print_formatted_text(HTML(f'<b fg="ansiblue">{name}</b>{dots}<i> {command.__doc__} </i>'))

    def run(self):
        print_formatted_text(HTML(f"<b>PolyTracker</b> ({version()})"))
        print_formatted_text(HTML('<u fg="ansigray">https://github.com/trailofbits/polytracker</u>'))
        print_formatted_text(
            HTML(
                'Type "<span fg="ansigreen" bg="ansigray">help</span>" or "<span fg="ansiblue" bg="ansigray">commands</span>"'
            )
        )
        prompt = HTML("<b>&gt;&gt;&gt; </b>")
        error_prompt = HTML('<span fg="ansired" bg="ansiwhite">!</span><b>&gt;&gt; </b>')
        next_prompt = prompt
        while True:
            try:
                completer = PolyTrackerCompleter(self)
                text = self.session.prompt(
                    next_prompt,
                    complete_while_typing=True,
                    auto_suggest=AutoSuggestFromHistory(),
                    completer=completer,
                    rprompt=completer.rprompt,
                    bottom_toolbar=completer.bottom_toolbar,
                    refresh_interval=0.5
                )
                next_prompt = prompt
            except KeyboardInterrupt:
                next_prompt = error_prompt
                continue
            except EOFError:
                break
            try:
                self.run_python(text)
            except NameError as e:
                print(str(e))
                next_prompt = error_prompt
            except SystemExit:
                break
            except:
                self.print_exc()
                next_prompt = error_prompt
        return 0


PolyTrackerREPL.register("commands")(PolyTrackerREPL.commands_command)


class Commands(Command):
    name = "commands"
    help = "print the PolyTracker commands"

    def run(self, args):
        longest_command = max(len(cmd_name) for cmd_name in COMMANDS)
        for command in COMMANDS.values():
            dots = "." * (longest_command - len(command.name) + 1)
            print_formatted_text(HTML(f'<b fg="ansiblue">{command.name}</b>{dots}<i> {command.help} </i>'))
