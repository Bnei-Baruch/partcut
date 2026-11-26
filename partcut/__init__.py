"""partcut - HQ lesson parts cut from converted full lessons."""
import os

# Must be set before numpy is imported anywhere: OpenBLAS's thread pool makes small ops ~2000x slower on this
# host and can hang when called from several Python threads (see parts.sh).
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
