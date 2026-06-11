from typing import Any


class Condition:

    def __init__(self, cond: str, value: bool, last_def: list[str]) -> None:
        self.__cond = cond
        self.__value = value
        self.__last_def = last_def

    def to_reqstr(self) -> str:
        req = f"// Precondition: {self.__cond} is {self.__value}"
        if self.__last_def:
            req += ", where "
            for defi in self.__last_def:
                req += defi + ", "
            req = req.rstrip(", ")
        req += ".\n"
        return req


class FunctionMock:

    def __init__(
        self,
        name: str,
        ret_type: str | None,
        ret_ref: bool,
        static: bool,
        virtual: bool,
        user: bool,
        actions: list[list[str]],
    ) -> None:
        self.__name = name
        self.__ret_type = ret_type
        self.__ret_ref = ret_ref
        self.__is_static = static
        self.__is_virtual = virtual
        self.__is_user = user
        self.__actions = actions

    def is_user(self) -> bool:
        return self.__is_user

    def is_virtual(self) -> bool:
        return self.__is_virtual

    def is_static(self) -> bool:
        return self.__is_static

    def to_reqstr(self) -> str:
        req = f"// Mock {self.__name}"
        if self.__ret_type:
            req += f" which returns a {self.__ret_type}."
            if self.__ret_ref:
                req += " You should use testing::ReturnRef() when the return type is a reference type."
        else:
            req += " which returns void."
        if self.__is_static:
            req += " It is a static function, you should mock it like Example B, and do not change the function signature."
        else:
            req += " It is a non-static function, you should mock it like Example A."
        if self.__actions:
            times = len(self.__actions)
            req += f" It should be called {times} time(s) with the following effect(s):"
            num = 0
            for action in self.__actions:
                num += 1
                if len(action) >= 1:
                    req += f" {num}. Make "
                    for condition in action:
                        req += condition + ", "
                else:
                    req += f" {num}. No effect"
                req = req.rstrip(", ")
        req += ".\n"
        return req.replace("_Bool", "bool")


class ClassMock:

    def __init__(self, name: str, file: str, is_class: bool) -> None:
        self.__name = name
        self.__file = file
        self.__is_class = is_class
        self.__is_user: None | bool = None
        self.__content: list[FunctionMock] = []

    # def add_function(self, function: FunctionMock) -> None:
    #     self.__content.append(function)

    def load_content(self, data: Any) -> None:
        for func_dict in data:
            func = FunctionMock(
                func_dict["function"],
                func_dict["return"],
                func_dict["return_ref"],
                func_dict["static"],
                func_dict["virtual"],
                func_dict["user"],
                func_dict["actions"],
            )
            self.__content.append(func)

    def get_file(self) -> str:
        return self.__file

    def is_class(self) -> bool:
        return self.__is_class

    def is_user(self) -> bool:
        if len(self.__content) == 0:
            raise Exception(f"No function in the class mock of {self.__name}")
        if self.__is_user is None:
            for function in self.__content:
                if not function.is_user():
                    self.__is_user = False
                    return False
            self.__is_user = True
            return True
        else:
            return self.__is_user

    def to_reqstr(self) -> str:
        req = ""
        mock_req = ""
        for function in self.__content:
            if (
                function.is_user()
                and function.is_virtual()
                and not function.is_static()
            ):
                mock_req += function.to_reqstr()
        if mock_req:
            if self.__is_class:
                req += f"// Create a mock class using gmock to mock {self.__name} in the test function.\n"
                req += mock_req
            # else:
            #     req += "// Using gmock to mock those free functions, like Example B, and do not change the signatures.\n"
            # req += mock_req
        return req


class CondChain:

    def __init__(
        self, chain_id: str, incatch: bool, min_cover: bool, result: str
    ) -> None:
        self.__id = chain_id
        self.__condtions: list[Condition] = []
        self.__mocks: list[ClassMock] = []
        self.__incatch = incatch
        self.__min_cover = min_cover
        self.__result = result
        self.__include_set: set[str] | None = None

    def get_id(self) -> str:
        return self.__id

    def get_includes(self) -> set[str]:
        if self.__include_set is None:
            raise Exception(
                f"Include set is not initialized for condition chain {self.__id}"
            )
        return self.__include_set

    # def add_condition(self, cond: Condition) -> None:
    #     self.__condtions.append(cond)

    def load_conditions(self, data: Any) -> None:
        for cond_dict in data:
            cond = Condition(
                cond_dict["condition"],
                cond_dict["value"],
                cond_dict["last_def"],
            )
            self.__condtions.append(cond)

    # def add_mock(self, mock: ClassMock) -> None:
    #     self.__mocks.append(mock)

    def load_mocks(self, data: Any) -> None:
        for class_name in data:
            mock_data = data[class_name]
            mock = ClassMock(
                class_name,
                mock_data["file"],
                class_name != "",
            )
            mock.load_content(mock_data["content"])

    def in_min_cover(self) -> bool:
        return self.__min_cover

    def to_reqstr(self) -> str:
        self.__include_set = set()
        req = ""
        for condition in self.__condtions:
            req += condition.to_reqstr()
        if self.__result:
            req += f"// Expected result: {self.__result}.\n"
        for mock in self.__mocks:
            mock_str = mock.to_reqstr()
            if mock_str and mock.is_class():
                self.__include_set.add(mock.get_file())
            req += mock_str
        return req.replace("_Bool", "bool")
