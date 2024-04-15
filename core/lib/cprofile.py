import cProfile
import functools
import os
import pstats


def do_cprofile(filepath=None):
    """
    Decorator for function profiling.
    """
    if not filepath:
        filepath = './prof'
    if not os.path.exists(filepath):
        os.makedirs(filepath)

    def wrapper(func):
        @functools.wraps(func)
        def profiled_func(*args, **kwargs):
            DO_PROF = os.getenv("PROFILING")
            if DO_PROF:
                profile = cProfile.Profile()
                profile.enable()
                result = func(*args, **kwargs)
                profile.disable()
                # Sort stat by internal time.
                sortby = "tottime"
                ps = pstats.Stats(profile).sort_stats(sortby)

                prof_file = func.__name__ + '.prof'
                img_file = func.__name__ + '.png'
                prof_file = os.path.join(filepath, prof_file)
                img_file = os.path.join(filepath, img_file)

                ps.dump_stats(prof_file)
                res = os.system(f"gprof2dot -f pstats {prof_file} | dot -Tpng -o {img_file}")
                if res == 0:
                    print('执行成功')
                else:
                    print('执行失败')
            else:
                result = func(*args, **kwargs)
            return result
        return profiled_func
    return wrapper
