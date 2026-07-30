/*
 * Ollie.app launcher.
 *
 * The obvious way to wrap a Python program in a .app is a shell script that
 * execs the interpreter. That does not work here: exec replaces the process
 * image, so macOS attributes Accessibility and Microphone to the interpreter
 * binary. The permission then shows up as "python 3.12" and covers every
 * program that interpreter ever runs — exactly the scoping we are trying to
 * avoid.
 *
 * So instead of exec'ing Python we *become* Python: this binary lives at
 * Contents/MacOS/Ollie, links libpython directly, and hands control to
 * Py_BytesMain. The process image never changes, so TCC sees the bundle and
 * the permission belongs to Ollie alone.
 *
 * Paths are baked in at build time by scripts/make_app.py.
 */

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern int Py_BytesMain(int argc, char **argv);

#ifndef OLLIE_PROJECT
#define OLLIE_PROJECT "."
#endif
#ifndef OLLIE_PYTHONHOME
#define OLLIE_PYTHONHOME ""
#endif
#ifndef OLLIE_SITE
#define OLLIE_SITE ""
#endif
#ifndef OLLIE_PYTHON
#define OLLIE_PYTHON ""
#endif

#define MAX_ARGS 64

/*
 * Python re-invokes its own executable for multiprocessing helpers, e.g.
 *   <exe> -c "from multiprocessing.resource_tracker import main;main(12)"
 * If we always injected "-m ollie" those children would relaunch the whole app
 * and die on unrecognised arguments. When the first argument looks like an
 * interpreter invocation we behave as a plain interpreter instead.
 */
static int is_interpreter_call(const char *arg) {
    static const char *flags[] = {"-c", "-m", "-E", "-S", "-I", "-u", "-X", "-B", "-O", "-"};
    for (size_t i = 0; i < sizeof(flags) / sizeof(flags[0]); i++) {
        if (strcmp(arg, flags[i]) == 0) {
            return 1;
        }
    }
    return arg[0] != '-';        /* a script path */
}

static void redirect_log(void) {
    const char *home = getenv("HOME");
    if (home == NULL) {
        return;
    }
    char dir[PATH_MAX];
    char path[PATH_MAX];
    snprintf(dir, sizeof(dir), "%s/.ollie", home);
    mkdir(dir, 0755);
    snprintf(path, sizeof(path), "%s/app.log", dir);

    if (freopen(path, "a", stdout) == NULL) {
        return;
    }
    freopen(path, "a", stderr);
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);

    time_t now = time(NULL);
    fprintf(stderr, "--- launching ollie at %s", ctime(&now));
}

int main(int argc, char **argv) {
    char exec_path[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(exec_path);
    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        snprintf(exec_path, sizeof(exec_path), "%s", argv[0]);
    }

    redirect_log();

    char pythonpath[PATH_MAX * 2];
    snprintf(pythonpath, sizeof(pythonpath), "%s:%s", OLLIE_PROJECT, OLLIE_SITE);

    /* PYTHONHOME finds the standard library; PYTHONPATH adds the project and
     * the virtualenv's site-packages, since PYTHONHOME disables venv detection. */
    setenv("PYTHONHOME", OLLIE_PYTHONHOME, 1);
    setenv("PYTHONPATH", pythonpath, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("OLLIE_BUNDLED", "1", 1);
    setenv("OLLIE_PYTHON", OLLIE_PYTHON, 1);

    if (chdir(OLLIE_PROJECT) != 0) {
        fprintf(stderr, "ollie: cannot enter %s\n", OLLIE_PROJECT);
        return 1;
    }

    /* Drop the process serial number LaunchServices sometimes appends. */
    char *args[MAX_ARGS];
    int count = 0;
    for (int i = 1; i < argc && count < MAX_ARGS - 6; i++) {
        if (strncmp(argv[i], "-psn_", 5) != 0) {
            args[count++] = argv[i];
        }
    }

    char *py_argv[MAX_ARGS];
    int n = 0;
    py_argv[n++] = exec_path;
    if (count > 0 && is_interpreter_call(args[0])) {
        for (int i = 0; i < count; i++) {
            py_argv[n++] = args[i];
        }
    } else {
        py_argv[n++] = "-u";
        py_argv[n++] = "-m";
        py_argv[n++] = "ollie";
        for (int i = 0; i < count; i++) {
            py_argv[n++] = args[i];
        }
    }
    py_argv[n] = NULL;

    return Py_BytesMain(n, py_argv);
}
