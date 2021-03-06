#!/usr/bin/env python

import networkx as nx
import Queue
import cPickle as pickle
import os
import sys
import socket
import time
from datetime import datetime
from subprocess import call
from shlex import split
from multiprocessing import Process, Event
import file_handling as fh
import pipeline_executor as pe
import logging
import threading
import Pyro4

LOOP_INTERVAL = 5.0
RESPONSE_LATENCY = 100

logger = logging.getLogger(__name__)

sys.excepthook = Pyro4.util.excepthook

class PipelineFile():
    def __init__(self, filename):
        self.filename = filename
        self.setType()
    def setType(self):
        self.fileType = None
    def __repr__(self):
        return(self.filename)

class InputFile(PipelineFile):
    def setType(self):
        self.fileType = "input"

class OutputFile(PipelineFile):
    def setType(self):
        self.fileType = "output"

class LogFile(PipelineFile):
    def setType(self):
        self.fileType = "log"

class PipelineStage():
    def __init__(self):
        self.mem = 2.0 # default memory allotted per stage
        self.procs = 1 # default number of processors per stage
        self.inputFiles = [] # the input files for this stage
        self.outputFiles = [] # the output files for this stage
        self.logFile = None # each stage should have only one log file
        self.status = None
        self.name = ""
        self.colour = "black" # used when a graph is created of all stages to colour the nodes
        self.number_retries = 0 

    def isFinished(self):
        return self.status == "finished"
    def setRunning(self):
        self.status = "running"
    def setFinished(self):
        self.status = "finished"
    def setFailed(self):
        self.status = "failed"
    def setNone(self):
        self.status = None
    def setMem(self, mem):
        self.mem = mem
    def getMem(self):
        return self.mem
    def setProcs(self, num):
        self.procs = num
    def getProcs(self):
        return self.procs
    def getHash(self):
        return(hash("".join(self.outputFiles) + "".join(self.inputFiles)))
    def __eq__(self, other):
        return self.inputFiles == other.inputFiles and self.outputFiles == other.outputFiles
    def __ne__(self, other):
        return not(__eq__(self,other))
    def getNumberOfRetries(self):
        return self.number_retries
    def incrementNumberOfRetries(self):
        self.number_retries += 1

class CmdStage(PipelineStage):
    def __init__(self, argArray):
        PipelineStage.__init__(self)
        self.argArray = argArray # the raw input array
        self.cmd = [] # the input array converted to strings
        self.parseArgs()
        self.checkLogFile()
    def parseArgs(self):
        if self.argArray:
            for a in self.argArray:
                ft = getattr(a, "fileType", None)
                if ft == "input":
                    self.inputFiles.append(str(a))
                elif ft == "output":
                    self.outputFiles.append(str(a))
                self.cmd.append(str(a))
                self.name = self.cmd[0]
    def checkLogFile(self):
        if not self.logFile:
            self.logFile = self.name + "." + datetime.isoformat(datetime.now()) + ".log"
    def setLogFile(self, logFileName): 
        self.logFile = str(logFileName)
    def execStage(self):
        of = open(self.logFile, 'a')
        of.write("Running on: " + socket.gethostname() + " at " + datetime.isoformat(datetime.now(), " ") + "\n")
        of.write(repr(self) + "\n")
        of.flush()

        if self.is_effectively_complete():
            of.write("All output files exist. Skipping stage.\n")
            returncode = 0
        else:
            args = split(repr(self)) 
            returncode = call(args, stdout=of, stderr=of, shell=False) 
        of.close()
        return(returncode)
    
    def is_effectively_complete(self):
        """check if this stage is effectively complete (if output files already exist)"""
        for output in self.outputFiles + self.inputFiles:
            if not os.path.exists(output):
                return False
        return True

    def getHash(self):
        return(hash(" ".join(self.cmd)))
    def __repr__(self):
        return(" ".join(self.cmd))

class Pipeline():
    def __init__(self):
        # the core pipeline is stored in a directed graph. The graph is made
        # up of integer indices
        self.G = nx.DiGraph()
        # an array of the actual stages (PipelineStage objects)
        self.stages = []
        self.nameArray = []
        # a queue of the stages ready to be run - contains indices
        self.runnable = Queue.Queue()
        # a list of currently running stages
        self.currently_running_stages = []
        # the current stage counter
        self.counter = 0
        # hash to keep the output to stage association
        self.outputhash = {}
        # a hash per stage - computed from inputs and outputs or whole command
        self.stagehash = {}
        # an array containing the status per stage
        self.processedStages = []
        # location of backup files for restart if needed
        self.backupFileLocation = None
        # list of registered clients
        self.clients = []
        # map from clients to jobs running on each client
        # TODO in principle this subsumes self.clients[] ...
        self.client_running_stages = {}
        self.clientTimestamps = {}
        # number of clients (executors) that have been launched by the server
        # we need to keep track of this because even though no (or few) clients
        # are actually registered, a whole bunch of them could be waiting in the
        # queue
        self.number_launched_and_waiting_clients = 0
        # clients we've lost contact with due to crash, etc.
        self.failed_executors = 0
        # main option hash, needed for the pipeline (server) to launch additional
        # executors during run time
        self.main_options_hash = None
        self.programName = None
        # Initially set number of skipped stages to be 0
        self.skipped_stages = 0
        self.failed_stages = 0
        self.verbose = 0

    def getTotalNumberOfStages(self):
        return len(self.stages)
    
    def getNumberProcessedStages(self):
        return len(self.processedStages)

    def getNumberOfRunningClients(self):
        return len(self.clients)

    def getNumberOfQueuedClients(self):
        return self.number_launched_and_waiting_clients

    def setVerbosity(self, verbosity):
        self.verbose = verbosity

    def getCurrentlyRunningStages(self):
        return self.currently_running_stages

    def getNumberRunnableStages(self):
        return self.runnable.qsize()
        
    def pipelineFullyCompleted(self):
        return (len(self.stages) == len(self.processedStages))
        
    def addStage(self, stage):
        """adds a stage to the pipeline"""
        # check if stage already exists in pipeline - if so, don't bother

        # check if stage exists - stage uniqueness defined by in- and outputs
        # for base stages and entire command for CmdStages
        h = stage.getHash()
        if self.stagehash.has_key(h):
            self.skipped_stages += 1 
            #stage already exists - nothing to be done
        else: #stage doesn't exist - add it
            # add hash to the dict
            self.stagehash[h] = self.counter
            #self.statusArray[self.counter] = 'notstarted'
            # add the stage itself to the array of stages
            self.stages.append(stage)
            self.nameArray.append(stage.name)
            # add all outputs to the output dictionary
            for o in stage.outputFiles:
                self.outputhash[o] = self.counter
            # add the stage's index to the graph
            self.G.add_node(self.counter, label=stage.name,color=stage.colour)
            # increment the counter for the next stage
            self.counter += 1
    def selfPickle(self):
        """Pickles pipeline in case future restart is needed"""
        if (self.backupFileLocation == None):
            self.setBackupFileLocation()
        pickle.dump(self.G, open(str(self.backupFileLocation) + '/G.pkl', 'wb'))
        pickle.dump(self.stages, open(str(self.backupFileLocation) + '/stages.pkl', 'wb'))
        pickle.dump(self.nameArray, open(str(self.backupFileLocation) + '/nameArray.pkl', 'wb'))
        pickle.dump(self.counter, open(str(self.backupFileLocation) + '/counter.pkl', 'wb'))
        pickle.dump(self.outputhash, open(str(self.backupFileLocation) + '/outputhash.pkl', 'wb'))
        pickle.dump(self.stagehash, open(str(self.backupFileLocation) + '/stagehash.pkl', 'wb'))
        pickle.dump(self.processedStages, open(str(self.backupFileLocation) + '/processedStages.pkl', 'wb'))
        logger.info("Pipeline pickled")
    def restart(self):
        """Restarts the pipeline from previously pickled backup files."""
        if (self.backupFileLocation == None):
            self.setBackupFileLocation()
            logger.info("Backup location not specified. Looking in the current directory.")
        try:
            self.G = pickle.load(open(str(self.backupFileLocation) + '/G.pkl', 'rb'))
            self.stages = pickle.load(open(str(self.backupFileLocation) + '/stages.pkl', 'rb'))
            self.nameArray = pickle.load(open(str(self.backupFileLocation) + '/nameArray.pkl', 'rb'))
            self.counter = pickle.load(open(str(self.backupFileLocation) + '/counter.pkl', 'rb'))
            self.outputhash = pickle.load(open(str(self.backupFileLocation) + '/outputhash.pkl', 'rb'))
            self.stagehash = pickle.load(open(str(self.backupFileLocation) + '/stagehash.pkl', 'rb'))
            self.processedStages = pickle.load(open(str(self.backupFileLocation) + '/processedStages.pkl', 'rb'))
            logger.info('Successfully reimported old data from backups.')
        except:
            logger.exception("Backup files are not recoverable.  Pipeline restart required.")
            sys.exit()

        done = []
        for i in self.G.nodes_iter():
            if self.stages[i].isFinished():
                done.append(i)
            else:
                if i in self.processedStages:
                    self.processedStages.remove(i)
        logger.info('Previously completed stages (of ' + str(len(self.stages)) + ' total): ' + str(len(done)))

    def setBackupFileLocation(self, outputDir=None):
        """Sets location of backup files."""
        if (outputDir == None):
            # set backups in current directory if directory doesn't currently exist
            outputDir = os.getcwd() 
        self.backupFileLocation = fh.createBackupDir(outputDir)   
    def addPipeline(self, p):
        if p.skipped_stages > 0:
            self.skipped_stages += p.skipped_stages
        for s in p.stages:
            self.addStage(s)
    def printStages(self, name):
        """Prints stages to a file, stage info to stdout"""
        fileForPrinting = os.path.abspath(os.curdir + "/" + str(name) + "-pipeline-stages.txt")
        pf = open(fileForPrinting, "w")
        for i in range(len(self.stages)):
            pf.write(str(i) + "  " + str(self.stages[i]) + "\n")
        pf.close()
        print "Total number of stages in the pipeline: ", len(self.stages)
                   
    def printNumberProcessedStages(self):
        print "Number of stages already processed:     ", len(self.processedStages)
                   
    def createEdges(self):
        """computes stage dependencies by examining their inputs/outputs"""
        starttime = time.time()
        # iterate over all nodes
        for i in self.G.nodes_iter():
            for ip in self.stages[i].inputFiles:
                # if the input to the current stage was the output of another
                # stage, add a directional dependence to the DiGraph
                if self.outputhash.has_key(ip):
                    self.G.add_edge(self.outputhash[ip], i)
        endtime = time.time()
        logger.info("Create Edges time: " + str(endtime-starttime))
    def computeGraphHeads(self):
        """adds stages with no incomplete predecessors to the runnable queue"""
        graphHeads = []
        for i in self.G.nodes_iter():
            if self.stages[i].isFinished() == False:
                """ either it has 0 predecessors """
                if len(self.G.predecessors(i)) == 0:
                    self.runnable.put(i)
                    graphHeads.append(i)
                """ or all of its predecessors are finished """
                if len(self.G.predecessors(i)) != 0:
                    predfinished = True
                    for j in self.G.predecessors(i):
                        if self.stages[j].isFinished() == False:
                            predfinished = False
                    if predfinished == True:
                        self.runnable.put(i) 
                        graphHeads.append(i)
        logger.info("Graph heads: " + str(graphHeads))
    def getStage(self, i):
        """given an index, return the actual pipelineStage object"""
        return(self.stages[i])
    # getStage<...> are currently used instead of getStage due to previous bug; could revert:
    def getStageMem(self, i):
        return(self.stages[i].mem)
    def getStageProcs(self,i):
        return(self.stages[i].procs)
    def getStageCommand(self,i):
        return(repr(self.stages[i]))
    def getStage_is_effectively_complete(self,i):
        return(self.stages[i].is_effectively_complete())
    def getStageLogfile(self,i):
        return(self.stages[i].logFile)

    """Given client information, issue commands to the client (along similar
    lines to getRunnableStageIndex) and update server's internal view of client.
    This is highly stateful, being a resource-tracking wrapper around
    getRunnableStageIndex and hence a glorified Queue().get()."""
    def getCommand(self, clientURIstr, clientMemFree, clientProcsFree):
        flag, i = self.getRunnableStageIndex()
        if flag == "run_stage":
            if ((self.getStageMem(i) <= clientMemFree) and (self.getStageProcs(i) <= clientProcsFree)):
                return (flag, i)
            else:
                logger.debug("Not enough resources for %s to run stage %d.", clientURIstr, i)
                self.requeue(i)
                # TODO search the queue for something this client can run?
                return ("wait", None)
        else:
            return (flag, i)

    """Return a tuple of a command ("shutdown_normally" if all stages are finished,
    "wait" if no stages are currently runnable, or "run_stage" if a stage is
    available) and the next runnable stage if the flag is "run_stage", otherwise
    None"""
    def getRunnableStageIndex(self):
        if self.allStagesComplete():
            return ("shutdown_normally", None)
        elif self.runnable.empty():
            return ("wait", None)
        else:
            index = self.runnable.get()
            return ("run_stage", index)

    def allStagesComplete(self):
        return len(self.processedStages) == len(self.stages)
        
    def setStageStarted(self, index, clientURI):
        URIstring = "(" + str(clientURI) + ")"
        logger.debug("Starting Stage " + str(index) + ": " + str(self.stages[index]) +
                     URIstring)
        self.client_running_stages[clientURI].add(index)
        self.currently_running_stages.append(index)
        self.stages[index].setRunning()

    def checkIfRunnable(self, index):
        """stage added to runnable queue if all predecessors finished"""
        canRun = True
        logger.debug("Checking if stage " + str(index) + " is runnable ...")
        if self.stages[index].isFinished():
            canRun = False
        else:
            for i in self.G.predecessors(index):              
                s = self.getStage(i)
                if not s.isFinished():
                    canRun = False
        logger.debug("Stage " + str(index) + " Runnable: " + str(canRun))
        return canRun

    def setStageFinished(self, index, clientURI, save_state = True, checking_pipeline_status = False):
        """given an index, sets corresponding stage to finished and adds successors to the runnable queue"""
        logger.info("Finished Stage " + str(index) + ": " + str(self.stages[index]))
        # this function can be called when a pipeline is restarted, and 
        # we go through all stages and set the finished ones to... finished... :-)
        # in that case, we can not remove the stage from the list of running
        # jobs, because there is none.
        if checking_pipeline_status:
            self.stages[index].status = "finished"
        else:
            self.removeFromRunning(index, clientURI, new_status = "finished")
        self.processedStages.append(index)
        # disableing the pickling for now. On larger pipelines this can take
        # 10-15 seconds after inital stages finished, which increases in duration
        # later on. 
        #if save_state: 
        #    self.selfPickle()
        for i in self.G.successors(index):
            if self.checkIfRunnable(i):
                self.runnable.put(i)

    def removeFromRunning(self, index, clientURI, new_status):
        self.currently_running_stages.remove(index)
        self.client_running_stages[clientURI].remove(index)
        self.stages[index].status = new_status

    def setStageLost(self, index, clientURI):
        """Clean up a stage lost due to unresponsive client"""
        self.removeFromRunning(index, clientURI, new_status = None)
        self.requeue(index)

    def setStageFailed(self, index, clientURI):
        # given an index, sets stage to failed, adds to processed stages array
        # But... only if this stage has already been retried twice (<- for now static)
        # Once in while retrying a stage makes sense, because of some odd I/O
        # read write issue (NFS race condition?). At least that's what I think is 
        # happening, so trying this to see whether it solves the issue.
        num_retries = self.stages[index].getNumberOfRetries()
        if num_retries < 2:
            self.removeFromRunning(index, clientURI, new_status = None)
            self.stages[index].incrementNumberOfRetries()
            logger.debug("RETRYING: ERROR in Stage " + str(index) + ": " + str(self.stages[index]))
            logger.debug("RETRYING: adding this stage back to the runnable queue.")
            logger.debug("RETRYING: Logfile for Stage " + str(self.stages[index].logFile))
            self.requeue(index)
        else:
            self.removeFromRunning(index, clientURI, new_status = "failed")
            logger.info("ERROR in Stage " + str(index) + ": " + str(self.stages[index]))
            # This is something we should also directly report back to the user:
            print("\nERROR in Stage %s: %s" % (str(index), str(self.stages[index])))
            print("Logfile for (potentially) more information:\n%s\n" % self.stages[index].logFile)
            sys.stdout.flush()
            self.processedStages.append(index)
            self.failed_stages += 1
            for i in nx.dfs_successor(self.G, index).keys():
                self.processedStages.append(i)

    def requeue(self, i):
        """If stage cannot be run due to insufficient mem/procs, executor returns it to the queue"""
        logger.debug("Requeueing stage %d", i)
        self.runnable.put(i)

    def initialize(self):
        """called once all stages have been added - computes dependencies and adds graph heads to runnable queue"""
        self.runnable = Queue.Queue()
        self.createEdges()
        self.computeGraphHeads()
        
    """
        Returns True unless all stages are finished, then False
        
        This function also checks to see whether executors can be launched. The
        server keeps track of how many executors are launched/registered. If the
        server set to launch executors itself, it will do so if there are runnable
        stages and the addition of launched/registered executors is smaller than
        the max number of executors it can launch
    """
    def continueLoop(self):
        # exit if there are still stages that need to be run, 
        # but when there are no runnable nor any running stages left
        if (self.runnable.empty() and
            len(self.currently_running_stages) == 0
            and self.failed_stages > 0):
            # nothing running, nothing can be run, but we're
            # also not done processing all stages
            logger.info("ERROR: no more runnable stages, however not all stages have finished. Going to shut down.")
            # This is something we should also directly report back to the user:
            print("\nERROR: no more runnable stages, however not all stages have finished. Going to shut down.\n")
            sys.stdout.flush()
            return False

        # TODO return False if all executors have died but not spawning new ones...

        if not self.allStagesComplete():
            return True
        #elif len(self.clients) > 0:
            # this branch is to allow clients asking for more jobs to shutdown
            # gracefully when the server has no more jobs
            # since it might hang the server if a client has become unresponsive
            # it's currently commented.  We might turn it back on once the server
            # has a way to detect unresponsive clients.
            # (also, before shutting down, we currently sleep for longer
            # than the interval between client connections in order for
            # clients to realize they need to shut down)
            # TODO what if a launched_and_waiting client registers here?
        #    return True
        else:
            return False

    def updateClientTimestamp(self, clientURI):
        self.clientTimestamps[clientURI] = time.time() # use server clock for consistency

    def mainLoop(self):
        while self.continueLoop():
            # check to see whether new executors need to be launched
            executors_to_launch = self.numberOfExecutorsToLaunch()
            if executors_to_launch > 0:
                self.launchExecutorsFromServer(executors_to_launch)

            # look for dead clients and requeue their jobs
            # copy() is used because otherwise client_running_stages may change size
            # during the iteration, throwing an exception,
            # but if we haven't heard from a client in some time,
            # that particular client isn't likely to be the source of the
            # change, so using a slightly stale copy shouldn't miss
            # anything interesting
            # FIXME there are potential race conditions here with
            # requeue, unregisterClient ... take locks for this whole section?
            t = time.time()
            for client, stages in self.client_running_stages.copy().iteritems():
                if t - self.clientTimestamps[client] > pe.HEARTBEAT_INTERVAL + RESPONSE_LATENCY:
                    logger.warn("Executor at %s has died!", client)
                    print("\nWarning: there has been no contact with %s, for %f seconds. Considering the executor as dead!\n" % (client, 5 + RESPONSE_LATENCY))
                    if self.failed_executors > self.main_options_hash.max_failed_executors:
                        logger.warn("Too many executors lost to spawn new ones")

                    self.failed_executors += 1

                    for s in stages.copy():
                        self.setStageLost(s,client)
                    self.unregisterClient(client)

            time.sleep(LOOP_INTERVAL)
        logger.debug("Server loop shutting down")
            
    """
        Returns an integer indicating the number of executors to launch
        
        This function first verifies whether the server can launch executors
        on its own (self.main_options_hash.nums_exec != 0). Then it checks to
        see whether the executors are able to kill themselves. If they are,
        it's possible that new executors need to be launched. This happens when
        there are runnable stages, but the number of active executors is smaller
        than the number of executors the server is able to launch
    """
    def numberOfExecutorsToLaunch(self):
        if self.failed_executors > self.main_options_hash.max_failed_executors:
            return 0

        executors_to_launch = 0
        if self.main_options_hash.num_exec != 0:
            # Server should launch executors itself
            # This should happen regardless of whether or not executors
            # can kill themselves, because the server is now responsible 
            # for the inital launches as well.
            active_executors = self.number_launched_and_waiting_clients + len(self.clients)
            max_num_executors = self.main_options_hash.num_exec
            executor_launch_room = max_num_executors - active_executors
            if self.runnable.qsize() > 0 and executor_launch_room > 0:
                # there are runnable stages, and there is room to launch 
                # additional executors
                executors_to_launch = min(self.runnable.qsize(), executor_launch_room)
        return executors_to_launch
        
    def launchExecutorsFromServer(self, number_to_launch):
        # As the function name suggests, here we launch executors!
        try:
            logger.debug("Launching %i executors", number_to_launch)
            processes = [Process(target=launchPipelineExecutor, args=(self.main_options_hash,self.programName,)) for i in range(number_to_launch)]
            for p in processes:
                p.start()
                self.incrementLaunchedClients()
        except:
            logger.exception("Failed launching executors from the server.")
        
    def getProcessedStageCount(self):
        return len(self.processedStages)

    def registerClient(self, client):
        # Adds new client (represented by a URI string)
        # to array of registered clients. If the server launched
        # its own clients, we should remove 1 from the number of launched and waiting
        # clients (It's possible though that users launch clients themselves. In that 
        # case we should not decrease this variable)
        self.clients.append(client)
        self.client_running_stages[client] = set([])
        self.clientTimestamps[client] = time.time()
        if self.number_launched_and_waiting_clients > 0:
            self.number_launched_and_waiting_clients -= 1
        if self.verbose:
            print("Client registered (banzai!): %s" % client)

    def unregisterClient(self, client):
        # removes a client URI string from the array of registered clients. An executor 
        # calls this method when it decides on its own to shut down,
        # and the server may call it when a client is unresponsive
        if client in self.clients:
            self.clients.remove(client)
            del self.client_running_stages[client]
            del self.clientTimestamps[client]
            if self.verbose:
                print("Client un-registered (seppuku!): " + client)
        else:
            if self.verbose:
                print("Unable to un-register client: " + client)

    def incrementLaunchedClients(self):
        self.number_launched_and_waiting_clients += 1

    def printShutdownMessage(self):
        # it is possible that pipeline.continueLoop returns false, even though the
        # pipeline is not completed (for instance, when stages failed, and no more stages
        # can be run) so check that in order to provide the correct feedback to the user
        print("\n\n######################################################")
        if self.pipelineFullyCompleted():
            print("All pipeline stages have been processed. \nDaemon unregistering " 
              + str(len(self.clients)) + " client(s) and shutting down...\n\n")
        else:
            print("Not all pipeline stages have been processed,")
            print("however there are no more stages that can be run.")
            print("Daemon unregistering " + str(len(self.clients)) + " client(s) and shutting down...\n\n\n")
        print("Objects successfully unregistered and daemon shutdown.")
        if self.pipelineFullyCompleted():
            print("Pipeline finished successfully!")
        else:
            print("Pipeline failed...")
        print("######################################################\n")
        sys.stdout.flush()

def launchPipelineExecutor(options, programName=None):
    """Launch pipeline executor directly from pipeline"""
    pipelineExecutor = pe.pipelineExecutor(options)
    if options.queue == "sge":
        pipelineExecutor.submitToQueue(programName) 
    else: 
        pe.launchExecutor(pipelineExecutor)    

def skip_completed_stages(pipeline):
    runnable = []
    while True:
        flag,i = pipeline.getRunnableStageIndex()
        if i == None:
            break
        
        s = pipeline.getStage(i)
        if not isinstance(s, CmdStage):
            runnable.append(i)
            continue
        
        if not s.is_effectively_complete():
            runnable.append(i)
            continue
        
        pipeline.setStageFinished(i, "fake_client_URI", save_state = False, checking_pipeline_status = True)
        logger.debug("skipping stage %i", i)
    
    for i in runnable:
        pipeline.requeue(i)
        
def launchServer(pipeline, options):
    # first follow up on the previously reported total number of 
    # stages in the pipeline with how many have already finished:
    pipeline.printNumberProcessedStages()
    
    # is the server going to be verbose or not?
    if options.verbose:
        def verboseprint(*args):
            # Print each argument separately so caller doesn't need to
            # stuff everything to be printed into a single string
            for arg in args:
                print arg,
            print
    else:   
        verboseprint = lambda *a: None
    
    # getIpAddress is similar to socket.gethostbyname(...) 
    # but uses a hack to attempt to avoid returning localhost (127....)
    network_address = Pyro4.socketutil.getIpAddress(socket.gethostname(),
                                                    workaround127 = True, ipVersion = 4)
    daemon = Pyro4.core.Daemon(host=network_address)
    pipelineURI = daemon.register(pipeline)
    
    if options.use_ns:
        # in the future we might want to launch a nameserver here
        # instead of relying on a separate executable running
        ns = Pyro4.locateNS()
        ns.register("pipeline", pipelineURI)
    else:
    # If not using Pyro NameServer, must write uri to file for reading by client.
        uf = open(options.urifile, 'w')
        uf.write(pipelineURI.asString())
        uf.close()
    
    # set the verbosity of the pipeline before running it
    pipeline.setVerbosity(options.verbose)
    verboseprint("Daemon is running at: %s" % daemon.locationStr)
    verboseprint("The pipeline's uri is: %s" % str(pipelineURI))

    try:
        t = threading.Thread(target=daemon.requestLoop)
        t.daemon = True
        t.start()
        pipeline.mainLoop()
    except KeyboardInterrupt:
        logger.exception("Caught keyboard interrupt, killing executors and shutting down server.")
        print("\nKeyboardInterrupt caught: cleaning up, shutting down executors.\n")
        sys.stdout.flush()
    except:
        logger.exception("Failed running server in daemon.requestLoop. Server shutting down.")
    finally:
        # allow time for all clients to contact the server and be told to shut down
        # (we could instead add a way for the server to notify its registered clients):
        # also, currently this doesn't happen until all jobs are finished (see getCommand),
        # but if we instead decided to shut down once the queue is empty, then
        # various notifyStageTerminated calls could fail
        time.sleep(pe.WAIT_TIMEOUT + 1)
        daemon.shutdown()
        t.join()
        pipeline.printShutdownMessage()

def flatten_pipeline(p):
    """return a list of tuples for each stage.
       Each item in the list is (id, command, [dependencies]) 
       where dependencies is a list of stages depend on this stage to be complete before they run.
    """
    def post(x, y):
        if y[0] in x[2]: 
            return 1
        elif x[0] in y[2]:
            return -1
        else:
            return 0 
                
    return sorted([(i, str(p.stages[i]), p.G.predecessors(i)) for i in p.G.nodes_iter()],cmp=post)

def sge_script(p):
    qsub = "sge_batch_hold -l vf=2G"
    flat = flatten_pipeline(p)

    subs   = []
    alter  = []
    unhold = []
    f = lambda x: "MAGeT_%i" % x

    script = []
    skipped_stages = 0
    for i in flat:
        job_id,cmd,depends = i
        stage  = p.getStage(job_id)
        if isinstance(stage, CmdStage): 
            if stage.is_effectively_complete():
                skipped_stages += 1
                continue
        name = f(job_id)
        deps = ",".join(map(f,depends))
        job_cmd="%s -J %s %s" % (qsub, name, cmd)
        script.append(job_cmd)
        if depends:
            depend_cmd="qalter -hold_jid %s %s" % (deps,name)
            script.append(depend_cmd)
        unhold_cmd = "qalter -h U %s" % name
        script.append(unhold_cmd)
    
    print skipped_stages, "stages skipped (outputs exist).", len(subs), "stages to run."
    return script #subs + alter + unhold

def pipelineDaemon(pipeline, options=None, programName=None):
    """Launches Pyro server and (if specified by options) pipeline executors"""

    #check for valid pipeline 
    if pipeline.runnable.empty():
        print "Pipeline has no runnable stages. Exiting..."
        sys.exit()

    if options.queue == "sge_script":
        script = open("sge_script", "w")
        script.write("\n".join(sge_script(pipeline)))
        script.close()
        print "SGE job submission script for this pipeline written to sge_script"
        sys.exit()

    if options.urifile==None:
        options.urifile = os.path.abspath(os.curdir + "/" + "uri")
        
    logger.debug("Examining filesystem to determine skippable stages...")
    skip_completed_stages(pipeline)
    
    logger.debug("Prior to starting server, total stages %i. Number processed: %i.", 
                 len(pipeline.stages), len(pipeline.processedStages))
    logger.debug("Number of stages in runnable index (size of queue): %i",
                 pipeline.runnable.qsize())
                 
    # provide the pipeline with the main option hash. The server when started 
    # needs access to information in there in order to (re)launch executors
    # during run time
    pipeline.main_options_hash = options
    pipeline.programName = programName
    logger.debug("Starting server...")
    process = Process(target=launchServer, args=(pipeline,options))
    process.start()
  
    try:
        process.join()
    except KeyboardInterrupt:
        print "\nCaught KeyboardInterrupt; exiting\n"
        sys.exit(0)
