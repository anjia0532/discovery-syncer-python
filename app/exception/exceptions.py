from core.model.handler import Resp, ErrCode


class ExceptionError(Exception):
    """自定义错误类型基类"""
    CODE = ErrCode.ERROR

    def __init__(self, message: str, code: ErrCode = None):
        self.message = message
        if code:
            self.code = code
        else:
            self.code = self.CODE


class ParamError(ExceptionError):
    """参数错误"""
    CODE = ErrCode.PARAM_ERR


class FileNotFound(ExceptionError):
    """文件不存在"""
    CODE = ErrCode.FileNotFound

