# -*- coding: utf-8 -*-

import signal
import sys, time, os, threading, multiprocessing
from itertools import chain, filterfalse
from collections import defaultdict
from snakemake.exceptions import TerminatedException, MissingOutputException, RuleException, \
	ClusterJobException, print_exception, format_error, get_exception_origin
from snakemake.shell import shell
from snakemake.io import IOFile, temp, protected, expand, touch, remove
from snakemake.utils import listfiles
from snakemake.logging import logger
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

if os.name == "posix":
	from multiprocessing import Event
	PoolExecutor = ProcessPoolExecutor
else:
	from threading import Event
	PoolExecutor = ThreadPoolExecutor

__author__ = "Johannes Köster"

def run_wrapper(run, rulename, ruledesc, input, output, wildcards, 
		threads, log, rowmaps, rulelineno, rulesnakefile):
	"""
	Wrapper around the run method that handles directory creation and
	output file deletion on error.
	
	Arguments
	run -- the run method
	input -- list of input files
	output -- list of output files
	wildcards -- so far processed wildcards
	"""
	logger.info(ruledesc)

			
	t0 = time.time()
	try:
		# execute the actual run method.
		run(input, output, wildcards, threads, log)
		# finish all spawned shells.
		shell.join_all()
		runtime = time.time() - t0
		return runtime
	except (Exception, BaseException) as ex:
		# this ensures that exception can be re-raised in the parent thread
		lineno, file = get_exception_origin(ex, rowmaps)
		raise RuleException(format_error(ex, lineno, rowmaps=rowmaps, snakefile=file), )

class Job:
	count = 0

	@staticmethod
	def cleanup_unfinished(jobs):
		for job in jobs:
			job.cleanup()

	def __init__(self, workflow, rule = None, message = None, reason = None,
			input = None, output = None, wildcards = None, shellcmd = None,
			threads = 1, log = None, depends = set(), dryrun = False, quiet = False,
			touch = False, needrun = True, pseudo = False, visited = None, dynamic_output = False, forced = False):
		self.workflow = workflow
		self.scheduler = None
		self.rule = rule
		self.message = message
		self.reason = reason
		self.input = input
		self.output = output
		self.wildcards = wildcards
		self.threads = threads
		self.log = log
		self.dryrun = dryrun
		self.quiet = quiet
		self.touch = touch
		self.shellcmd = shellcmd
		self.needrun = needrun
		self.pseudo = pseudo
		self.forced = forced
		self.dynamic_output = dynamic_output
		self.depends = set(depends)
		self.depending = list()
		self.is_finished = False
		self._callbacks = list()
		self._error_callbacks = list()
		self.jobid = Job.count
		self.visited = visited
		self.ignore = False
		Job.count += 1
		for other in self.depends:
			other.depending.append(self)

	def all_jobs(self):
		yield self
		for job in self.descendants():
			yield job

	def descendants(self):
		for job in self.depends:
			yield job
			for j in job.descendants():
				yield j

	def ancestors(self):
		queue = [job for job in self.depending]
		visited = set(queue)
		while queue:
			job = queue.pop(0)
			for j in job.depending:
				if j not in visited:
					queue.append(j)
					visited.add(j)
			yield job
	
	def get_message(self):
		msg = ""
		if not self.quiet:
			if self.message:
				msg += self.message.format(input=self.input, output=self.output, wildcards=self.wildcards, threads=self.threads, log=self.log, **globals())
			else:
				def showtype(orig_iofiles, iofiles):
					for i, iofile in enumerate(iofiles):
						if self.rule.is_dynamic(iofile):
							f = str(orig_iofiles[i])
							f += " (dynamic)"
						else:
							f = str(iofile)
						if iofile.is_temp():
							f += " (temporary)"
						if iofile.is_protected():
							f += " (protected)"
						yield f
				
				msg += "rule " + self.rule.name
				if self.input or self.output:
					msg += ":"
				if self.input:
					msg += "\n\tinput: {}".format(", ".join(showtype(self.rule.input, self.input)))
				if self.output:
					msg += "\n\toutput: {}".format(", ".join(showtype(self.rule.output, self.output)))
				if self.reason:
					msg += "\n\t{}".format(self.reason)
		if self.shellcmd:
			if not self.quiet:
				msg += "\n"
			msg += self.shellcmd.format(input=self.input, output=self.output, wildcards=self.wildcards, threads=self.threads, log=self.log, **globals())
		return msg
		
	def print_message(self):
		logger.info(self.get_message())
		
	def run(self, run_func):
		if not self.needrun or self.pseudo or self.ignore:
			self.finished()
		elif self.dryrun:
			self.print_message()
			self.finished()
		elif self.touch:
			logger.info(self.message)
			for o in self.output:
				if self.rule.is_dynamic(o):
					for f, _ in listfiles(o):
						touch(f)
				else:
					o.touch(self.rule.name, self.rule.lineno, self.rule.snakefile)
			# sleep shortly to ensure that output files of different rules 
			# are not touched at the same time.
			time.sleep(0.1)
			self.finished()
		else:
			for o in self.output:
				if self.rule.is_dynamic(o):
					for f, _ in listfiles(o):
						try:
							IOFile(f).remove()
						except OSError:
							raise RuleException("Could not remove dynamic output file {}.".format(f), lineno=self.rule.lineno, snakefile=self.rule.snakefile)
				# TODO what if a directory inside o is dynamic?
				o.prepare()
			if self.log:
				self.log.prepare()

			run_func(self)
	
	def get_run_args(self):
		return (self.rule.get_run(), self.rule.name, self.get_message(), 
			self.input, self.output, self.wildcards, self.threads, self.log, 
			self.workflow.rowmaps, self.rule.lineno, self.rule.snakefile)
	
	def add_callback(self, callback):
		""" Add a callback that is invoked when job is finished. """
		self._callbacks.append(callback)

	def add_error_callback(self, callback):
		self._error_callbacks.append(callback)
	
	def finished(self, future = None):
		""" Set job to be finished. """
		self.is_finished = True
		if not self.ignore:
			if self.needrun and not self.pseudo:
				try:
					if future:
						ex = future.exception()
						if ex:
							raise ex

					if not self.dryrun:
						# check the produced files
						for o in self.output:
							if not self.rule.is_dynamic(o):
								o.created(self.rule.name, self.rule.lineno, self.rule.snakefile)
						for f in self.input:
							f.used()
				except (Exception, BaseException) as ex:
					# in case of an error, execute all callbacks and delete output
					print_exception(ex, self.workflow.rowmaps)
					self.cleanup()
					for callback in self._error_callbacks:
						callback()
					return

				if not self.dryrun:
					self.workflow.jobcounter.done()
					if not self.quiet:
						logger.info(self.workflow.jobcounter)
					if not future is None:
						self.workflow.report_runtime(self.rule, future.result())

			for other in self.depending:
				other.depends.remove(self)

			if not self.dryrun and self.dynamic_output:
				self.handle_dynamic_output()	

		for callback in self._callbacks:
			callback(self)

	def cleanup(self):
		if not self.is_finished:
			for o in self.output:
				if self.rule.is_dynamic(o):
					for f, _ in listfiles(o):
						remove(f)
				else:
					o.remove()
				
	def handle_dynamic_output(self):
		wildcard_expansion = defaultdict(set)
		for i, o in enumerate(self.output):
			if self.rule.is_dynamic(o):
				for f, wildcards in listfiles(self.rule.output[i]):
					for name, value in wildcards.items():
						wildcard_expansion[name].add(value)
		# determine jobs to add
		new_jobs = set()
		dynamic = 0
		jobs = {(self.output, self.rule): self} # TODO add current non-dynamic jobs here?
		for job in self.ancestors():
			j = job.handle_dynamic_input(wildcard_expansion, jobs)
			if j:
				new_jobs.update(j.all_jobs())
				dynamic += 1
		# remove this job from the DAG as it would induce the dynamic loop again
		# TODO better set needrun = False above!
		if self in new_jobs:
			new_jobs.remove(self)
			for job in self.depending:
				try:
					job.depends.remove(self)
				except:
					pass

		# calculate how many jobs have to be added
		n = len(new_jobs) - dynamic
		if n:
			logger.warning("Dynamically adding {} new jobs".format(n))
		self.scheduler.add_jobs(new_jobs)
		self.workflow.jobcounter.count += n

	def handle_dynamic_input(self, wildcard_expansion, jobs):
		expansion = defaultdict(list)
		for i, f in enumerate(self.rule.input):
			if self.rule.is_dynamic(f):
				try:
					for e in reversed(expand(f, zip, **wildcard_expansion)):
						expansion[i].append(IOFile.create(e, temp = f.is_temp(), protected = f.is_protected()))
				except Exception as ex:
					# keep the file if expansion fails
					return
		if not expansion:
			return
		# replace the dynamic input with the expanded files
		for i, e in reversed(list(expansion.items())):
			self.rule.set_dynamic(self.rule.input[i], False)
			self.rule.input[i:i+1] = e
		try:
			# TODO what if self.output[0] is dynamic?
			job = self.rule.run(self.output[0] if self.output else None, jobs=jobs, forcethis=self.forced)

			# remove current job from DAG
			for j in self.depends:
				j.depending.remove(self)
			for j in self.depending:
				j.depends.remove(self)
				# TODO handle depending jobs!
				#j.depends.add(job)
				#job.depending.add(j)
			self.depends = list()
			self.ignore = True

			return job
		except Exception as ex:
			# there seem to be missing files, so ignore this
			pass
		

	def dot(self):
		label = self.rule.name
		new_wildcards = self.new_wildcards()
		if not self.depends or new_wildcards:
			for wildcard, value in new_wildcards:
				label += "\\n{}: {}".format(wildcard, value)
		edges = ("{} -> {};".format(j.jobid, self.jobid) 
			for j in self.depends if j.needrun)
		node = ('{}[label = "{}"];'.format(self.jobid, label),)
		return chain(node, edges)

	def new_wildcards(self):
		new_wildcards = set(self.wildcards.items())
		for job in self.depends:
			if not new_wildcards:
				return set()
			for wildcard in job.wildcards.items():
				new_wildcards.discard(wildcard)
		return new_wildcards

	def __repr__(self):
		return self.rule.name


class KnapsackJobScheduler:
	def __init__(self, jobs, workflow):
		""" Create a new instance of KnapsackJobScheduler. """
		self.workflow = workflow
		self._maxcores = workflow.cores if workflow.cores else multiprocessing.cpu_count()
		self._cores = self._maxcores
		self._pool = PoolExecutor(max_workers = self._cores)
		self._jobs = set()
		self.add_jobs(jobs)
		self._open_jobs = Event()
		self._open_jobs.set()
		self._errors = False

	def add_jobs(self, jobs):
		for job in jobs:
			job.scheduler = self
			self._jobs.add(job)

	def schedule(self):
		""" Schedule jobs that are ready, maximizing cpu usage. """
		while True:
			self._open_jobs.wait()
			self._open_jobs.clear()
			if self._errors:
				logger.warning("Will exit after finishing currently running jobs.")
				self._pool.shutdown()
				return False
			if not self._jobs:
				self._pool.shutdown()
				return True

			needrun, norun = [], set()
			for job in self._jobs:
				if job.depends:
					continue
				if job.needrun:
					if job.threads > self._maxcores:
						# reduce the number of threads so that it 
						# fits to available cores.
						if not job.dryrun:
							logger.warn(
								"Rule {} defines too many threads ({}), Scaling down to {}."
								.format(job.rule, job.threads, self._maxcores))
						job.threads = self._maxcores
					needrun.append(job)
				else: norun.add(job)
			
			run = self._knapsack(needrun)
			self._jobs -= run
			self._jobs -= norun
			self._cores -= sum(job.threads for job in run)
			for job in chain(run, norun):
				job.add_callback(self._finished)
				job.add_error_callback(self._error)
				job.run(self._run_job)
			
	
	def _run_job(self, job):
		future = self._pool.submit(run_wrapper, *job.get_run_args())
		future.add_done_callback(job.finished)
		
	def _finished(self, job):
		if job.needrun:
			self._cores += job.threads
		self._open_jobs.set()
	
	def _error(self):
		# clear jobs and stop the workflow
		self._errors = True
		self._jobs = set()
		self._open_jobs.set()
	
	def _knapsack(self, jobs):
		""" Solve 0-1 knapsack to maximize cpu utilization. """
		dimi, dimj = len(jobs) + 1, self._cores + 1
		K = [[0 for c in range(dimj)] for i in range(dimi)]
		for i in range(1, dimi):
			for j in range(1, dimj):
				t = jobs[i-1].threads
				if t > j:
					K[i][j] = K[i - 1][j]
				else:
					K[i][j] = max(K[i - 1][j], t + K[i - 1][j - t])
		
		solution = set()
		i = dimi - 1
		j = dimj - 1
		while i > 0:
			if K[i][j] != K[i-1][j]:
				job = jobs[i - 1]
				solution.add(job)
				j = j - job.threads
			i -= 1
		
		return solution

class ClusterJobScheduler:
	def __init__(self, jobs, workflow, submitcmd = "qsub"):
		self.workflow = workflow
		self._jobs = set()
		self.add_jobs(jobs)
		self._submitcmd = submitcmd
		self._open_jobs = Event()
		self._open_jobs.set()
		self._error = False
		self._cores = workflow.cores

	def add_jobs(self, jobs):
		for job in jobs:
			job.scheduler = self
			self._jobs.add(job)

	def schedule(self):
		while True:
			self._open_jobs.wait()
			self._open_jobs.clear()
			if self._error:
				logger.warning("Will exit after finishing currently running jobs.")
				return False
			if not self._jobs:
				return True
			needrun, norun = set(), set()
			for job in self._jobs:
				if job.depends:
					continue
				if job.needrun:
					needrun.add(job)
				else: norun.add(job)

			self._jobs -= needrun
			self._jobs -= norun
			for job in chain(needrun, norun):
				job.add_callback(self._finished)
				job.run(self._run_job)
	
	def _run_job(self, job):
		job.print_message()
		workdir = os.getcwd()
		prefix = ".snakemake.{}.".format(job.rule.name)
		jobid = "_".join(job.output).replace("/", "_")
		jobscript = "{}.{}.sh".format(prefix, jobid)
		jobfinished = "{}.{}.jobfinished".format(prefix, jobid)
		jobfailed = "{}.{}.jobfailed".format(prefix, jobid)
		cores = self._cores if self._cores else ""
		scriptpath = self.workflow.scriptpath
		if not scriptpath:
			scriptpath = "snakemake"
		shell("""
			echo '#!/bin/sh' > "{jobscript}"
			echo '#rule: {job}' >> "{jobscript}"
			echo '#input: {job.input}' >> "{jobscript}"
			echo '#output: {job.output}' >> "{jobscript}"
			echo '{scriptpath} --force -j{self._cores} --directory {workdir} --nocolor --quiet {job.output} && touch "{jobfinished}" || touch "{jobfailed}"' >> "{jobscript}"
			chmod +x "{jobscript}"
			{self._submitcmd} "{jobscript}"
		""")
		threading.Thread(target=self._wait_for_job, args=(job, jobfinished, jobfailed, jobscript)).start()

	def _finished(self, job):
		self._open_jobs.set()
		
	def _wait_for_job(self, job, jobfinished, jobfailed, jobscript):
		while True:
			if os.path.exists(jobfinished):
				os.remove(jobfinished)
				os.remove(jobscript)
				job.finished()
				return
			if os.path.exists(jobfailed):
				os.remove(jobfailed)
				os.remove(jobscript)
				print_exception(ClusterJobException(job), self.workflow.rowmaps)
				self._error = True
				self._jobs = set()
				self._open_jobs.set()
				return
			time.sleep(1)

def print_job_dag(jobs):
	print("digraph snakemake_dag {")
	for job in jobs:
		for edge in job.dot():
			print("\t" + edge)
	print("}")
