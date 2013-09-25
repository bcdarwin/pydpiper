#!/usr/bin/env python

from os.path import abspath
from optparse import OptionGroup
from datetime import date
from pydpiper.pipeline import Pipeline
from pydpiper.file_handling import createLogFile, createSubDir, makedirsIgnoreExisting, removeBaseAndExtension
from pydpiper.application import AbstractApplication
from pydpiper_apps.minc_tools.registration_file_handling import RegistrationPipeFH
from pydpiper_apps.minc_tools.registration_functions import addGenRegOptionGroup, initializeInputFiles
from pydpiper_apps.minc_tools.minc_atoms import blur, mincresample, mincANTS, mincAverage, minctracc
from pydpiper_apps.minc_tools.stats_tools import addStatsOptions, CalcStats
from pyminc.volumes.factory import volumeFromFile
import sys
import csv
import logging

logger = logging.getLogger(__name__)

def addNlinRegOptionGroup(parser):
    """option group for the command line argument parser"""
    group = OptionGroup(parser, "Nonlinear registration options",
                        "Options for performing a non-linear registration")
    group.add_option("--lsq12-avg", dest="lsq12_avg",
                     type="string", default=None,
                     help="Starting target for non-linear alignment.")
    group.add_option("--lsq12-mask", dest="lsq12_mask",
                     type="string", default=None,
                     help="Optional mask for target.")
    group.add_option("--nlin-protocol", dest="nlin_protocol",
                     type="string", default=None,
                     help="Can optionally specify a nonlinear protocol that is different from defaults. Default is None.")
    parser.add_option_group(group)

class NonlinearRegistration(AbstractApplication):
    def setup_options(self):
        """Add option groups from specific modules"""
        addNlinRegOptionGroup(self.parser)
        addGenRegOptionGroup(self.parser)
        addStatsOptions(self.parser)
        
        self.parser.set_usage("%prog [options] input files") 

    def setup_appName(self):
        appName = "Nonlinear-registration"
        return appName

    def run(self):
        options = self.options
        args = self.args
        
        """NOTE: Some of this code below is duplicated from MBM.py. 
           We'll want to condense into a function!"""
           
        """Make main pipeline directories"""
        pipeDir = makedirsIgnoreExisting(options.pipeline_dir)
        if not options.pipeline_name:
            pipeName = str(date.today()) + "_pipeline"
        else:
            pipeName = options.pipeline_name
        nlinDirectory = createSubDir(pipeDir, pipeName + "_nlin")
        processedDirectory = createSubDir(pipeDir, pipeName + "_processed")
        
        """Initialize input files (from args) and initial target"""
        inputFiles = initializeInputFiles(args, processedDirectory, maskDir=options.mask_dir)
        initialTarget = RegistrationPipeFH(options.lsq12_avg, mask=options.lsq12_mask, basedir=nlinDirectory)
        
        """Based on cmdline option, register with minctracc or mincANTS"""
        if options.reg_method=="mincANTS":
            ants = NLINANTS(inputFiles, initialTarget, nlinDirectory, options.nlin_protocol)
            ants.iterate()
            self.pipeline.addPipeline(ants.p)
            self.nlinAvg = ants.nlinAvg
        elif options.reg_method == "minctracc":
            tracc = NLINminctracc(inputFiles, initialTarget, nlinDirectory, options.nlin_protocol)
            tracc.iterate()
            self.pipeline.addPipeline(tracc.p)
            self.nlinAvg = tracc.nlinAvg
        else:
            logger.error("Incorrect registration method specified: " + options.reg_method)
            sys.exit()
            
        """Calculate statistics between final nlin average and individual mice"""
        if options.calc_stats:
            """Get blurs from command line option and put into array"""
            blurs = []
            for i in options.stats_kernels.split(","):
                blurs.append(float(i))
            """Choose final average from array of nlin averages"""
            numGens = len(self.nlinAvg)
            finalNlin = self.nlinAvg[numGens-1]
            """Hack to set lastXfms until I setup proper stats group"""
            for inputFH in inputFiles:
                inputFH.setLastXfm(finalNlin, inputFH.getLastXfm(self.nlinAvg[numGens-2]))
            """For each input file, calculate statistics"""
            for inputFH in inputFiles:
                stats = CalcStats(inputFH, finalNlin, blurs, inputFiles)
                stats.fullStatsCalc()
                self.pipeline.addPipeline(stats.p)
            
class NLINANTS(object):
    def __init__(self, inputArray, targetFH, nlinOutputDir, nlin_protocol=None):
        self.p = Pipeline()
        """Initial inputs should be an array of fileHandlers with lastBasevol in lsq12 space"""
        self.inputs = inputArray
        """Initial target should be the file handler for the lsq12 average"""
        self.target = targetFH 
        """Output directory should be _nlin """
        self.nlinDir = nlinOutputDir
        """Empty array that we will fill with averages as we create them"""
        self.nlinAvg = [] 
        """Create the blurring resolution from the file resolution"""
        self.ANTSBlur = volumeFromFile(self.target.getLastBasevol()).separations[0]
        
        """Below are ANTS parameters for each generation that are
           defaults for this alignment. If a non linear protocol is specified,
           some or all of these defaults may be overridden. 
           
           Note that for each generation, the blurs, gradient, similarity_metric,
           weight and radius/histogram are arrays. This is to allow for the one
           or more similarity metrics (and their associated parameters) to be specified
           for each mincANTS call. We typically use two, but the mincANTS atom allows
           for more or less if desired. 
        """
        self.blurs = [[-1, self.ANTSBlur], [-1, self.ANTSBlur],[-1, self.ANTSBlur]] 
        self.gradient = [[False,True], [False,True], [False,True]]
        self.similarityMetric = [["CC", "CC"],["CC", "CC"],["CC", "CC"]]
        self.weight = [[1,1],[1,1],[1,1]]
        self.radiusHisto = [[3,3],[3,3],[3,3]]
        self.transformationModel = ["SyN[0.5]", "SyN[0.4]", "SyN[0.4]"]
        self.regularization = ["Gauss[5,1]", "Gauss[5,1]", "Gauss[5,1]"]
        self.iterations = ["100x100x100x0", "100x100x100x20", "100x100x100x50"]
        self.useMask = [False, True, True]
        
        """If a non-linear protocol was specified, use this to override the
           default non-linear registration parameters. Currently, this protocol must
           be a csv file that uses a SEMI-COLON to separate the fields. An example is:
           pydpiper_apps_testing/test_data/mincANTS_example_nlin_protocol.csv 
           
           Each row in the csv is a different input to the mincANTS call (e.g. iterations, regularization, etc)
           Although the number of entries in each row (e.g. generations) is variable, the
           specific parameters are fixed. For example, one could specify a subset of the
           allowed parameters (e.g. similarity_metric only) but could not rename any parameters
           or use additional ones that haven't already been defined without subclassing. See
           documentation for additional details. 
           
           If a non-linear protocol is not specified, default parameters will be used."""
        if nlin_protocol:
            self.setParams(nlin_protocol)
            
        """Get number of generations based on length of parameter arrays. This is based on
           either defaults or whatever was specified from an external non-linear protocol.
           If the number of generations in each array does not match, an error is thrown and
           the program exits. """
        self.generations = self.getGenerations()
    
    def getGenerations(self):
        arrayLength = len(self.blurs)
        errorMsg = "Array lengths in non-linear mincANTS protocol do not match."
        if (len(self.gradient) != arrayLength 
            or len(self.similarityMetric) != arrayLength
            or len(self.weight) != arrayLength
            or len(self.radiusHisto) != arrayLength
            or len(self.transformationModel) != arrayLength
            or len(self.regularization) != arrayLength
            or len(self.iterations) != arrayLength
            or len(self.useMask) != arrayLength):
            print errorMsg
            raise
        else:
            return arrayLength
            
    def setParams(self, nlin_protocol):
        """Set parameters from specified protocol"""
        
        """Read parameters into array from csv."""
        inputCsv = open(abspath(nlin_protocol), 'rb')
        csvReader = csv.reader(inputCsv, delimiter=';', skipinitialspace=True)
        params = []
        for r in csvReader:
            params.append(r)
        """initialize arrays """
        self.blurs = []
        self.gradient = []
        self.similarityMetric = []
        self.weight = []
        self.radiusHisto = []
        self.transformationModel = []
        self.regularization = []
        self.iterations = []
        self.useMask = []
        """Parse through rows and assign appropriate values to each parameter array.
           Everything is read in as strings, but in some cases, must be converted to 
           floats, booleans or gradients. 
        """
        for p in params:
            if p[0]=="blur":
                """Blurs must be converted to floats."""
                for i in range(1,len(p)):
                    b = []
                    for j in p[i].split(","):
                        b.append(float(j)) 
                    self.blurs.append(b)
            elif p[0]=="gradient":
                """Gradients must be converted to bools."""
                for i in range(1,len(p)):
                    g = []
                    for j in p[i].split(","):
                        if j=="True" or j=="TRUE":
                            g.append(True)
                        elif j=="False" or j=="FALSE":
                            g.append(False)
                    self.gradient.append(g)
            elif p[0]=="similarity_metric":
                """Similarity metric does not need to be converted, but must be stored as an array for each generation."""
                for i in range(1,len(p)):
                    g = []
                    for j in p[i].split(","):
                        g.append(j)
                    self.similarityMetric.append(g)
            elif p[0]=="weight":
                """Weights are strings but must be converted to an int."""
                for i in range(1,len(p)):
                    w = []
                    for j in p[i].split(","):
                        w.append(int(j)) 
                    self.weight.append(w)
            elif p[0]=="radius_or_histo":
                """The radius or histogram parameter is a string, but must be converted to an int"""
                for i in range(1,len(p)):
                    r = []
                    for j in p[i].split(","):
                        r.append(int(j)) 
                    self.radiusHisto.append(r)
            elif p[0]=="transformation":
                for i in range(1,len(p)):
                    self.transformationModel.append(p[i])
            elif p[0]=="regularization":
                for i in range(1,len(p)):
                    self.regularization.append(p[i])
            elif p[0]=="iterations":
                for i in range(1,len(p)):
                    self.iterations.append(p[i])
            elif p[0]=="useMask":
                for i in range(1,len(p)):
                    """useMask must be converted to a bool."""
                    if p[i] == "True" or p[i] == "TRUE":
                        self.useMask.append(True)
                    elif p[i] == "False" or p[i] == "FALSE":
                        self.useMask.append(False)
            else:
                print "Improper parameter specified for mincANTS protocol: " + str(p[0])
                print "Exiting..."
                sys.exit()
         
    def iterate(self):
        for i in range(self.generations):
            for j in self.blurs[i]:
                if j != -1:
                    tblur = blur(self.target, j, gradient=True)              
                    self.p.addStage(tblur)
            filesToAvg = []
            for inputFH in self.inputs:
                for j in self.blurs[i]:
                    if j != -1:
                        iblur = blur(inputFH, j, gradient=True)
                        self.p.addStage(iblur) 
                ma = mincANTS(inputFH, 
                              self.target, 
                              defaultDir="tmp",
                              blur=self.blurs[i],
                              gradient=self.gradient[i], 
                              similarity_metric=self.similarityMetric[i],
                              weight=self.weight[i], 
                              iterations=self.iterations[i],
                              radius_or_histo=self.radiusHisto[i],
                              transformation_model = self.transformationModel[i],
                              regularization=self.regularization[i],
                              useMask=self.useMask[i])
                self.p.addStage(ma)
                rs = mincresample(inputFH, self.target, likeFile=self.target, defaultDir="tmp", argArray=["-sinc"])
                #Do we need to resample any masks?
                filesToAvg.append(rs.outputFiles[0])
                self.p.addStage(rs)
            
            """Because we don't reset lastBasevol on each inputFH, call mincAverage with files only.
               We create fileHandler first though, so we have log directory.
               This solution seems a bit hackish--may want to modify?  
               Additionally, we are currently using the full RegistrationPipeFH class, but ultimately
               we'll want to create a third class that is somewhere between a full and base class. 
            """
            nlinOutput = abspath(self.nlinDir) + "/" + "nlin-%g.mnc" % (i+1)
            nlinFH = RegistrationPipeFH(nlinOutput, mask=self.target.getMask(), basedir=self.nlinDir)
            logBase = removeBaseAndExtension(nlinOutput)
            avgLog = createLogFile(nlinFH.logDir, logBase)
            avg = mincAverage(filesToAvg, nlinOutput, logFile=avgLog)
            self.p.addStage(avg)
            """Reset target for next iteration and add to array"""
            self.target = nlinFH
            self.nlinAvg.append(nlinFH)

class NLINminctracc(object):
    def __init__(self, inputArray, targetFH, nlinOutputDir, nlin_protocol=None):
        self.p = Pipeline()
        """Initial inputs should be an array of fileHandlers with lastBasevol in lsq12 space"""
        self.inputs = inputArray
        """Initial target should be the file handler for the lsq12 average"""
        self.target = targetFH 
        """Output directory should be _nlin """
        self.nlinDir = nlinOutputDir
        """Empty array that we will fill with averages as we create them"""
        self.nlinAvg = [] 
        """Blur and step size parameters will be created from the file resolution."""
        self.fileRes = volumeFromFile(self.target.getLastBasevol()).separations[0]    
        
        """ 
            Default minctracc parameters for 6 generations. 
            If a non linear protocol is specified, some or all of these defaults 
            may be overridden. 
        """
        self.blurs = [self.fileRes*5.0, self.fileRes*(10.0/3.0), self.fileRes*(10.0/3.0),
                      self.fileRes*(10.0/3.0), self.fileRes*(5.0/3.0), self.fileRes]
        self.stepSize = [self.fileRes*(35.0/3.0), self.fileRes*10.0, self.fileRes*(25.0/3.0),
                      self.fileRes*4.0, self.fileRes*2.0, self.fileRes]
        self.iterations = [20,6,8,8,8,8]
        self.simplex = [5,2,2,2,2,2]
        self.useGradient = [True, True, True, True, True, True]
        self.optimization = ["-use_simplex", "-use_simplex", "-use_simplex", "-use_simplex", 
                             "-use_simplex", "-use_simplex"]
        
        """If a non-linear protocol was specified, use this to override the
           default non-linear registration parameters. Currently, this protocol must
           be a csv file that uses a SEMI-COLON to separate the fields. An example is:
           pydpiper_apps_testing/test_data/minctracc_example_protocol.csv
           
           Each row in the csv is a different input to the minctracc call (e.g. blurs, iterations, etc)
           Although the number of entries in each row (e.g. generations) is variable, the
           specific parameters are fixed. For example, one could specify a subset of the
           allowed parameters (e.g. blurs only) but could not rename any parameters
           or use additional ones that haven't already been defined without subclassing. See
           documentation for additional details. 
           
           If a non-linear protocol is not specified, default parameters will be used."""
        
        if nlin_protocol:
            self.setParams(nlin_protocol)
            
        """Get number of generations based on length of parameter arrays. This is based on
           either defaults or whatever was specified from an external non-linear protocol.
           If the number of generations in each array does not match, an error is thrown and
           the program exits. """
        self.generations = self.getGenerations()
    
    def getGenerations(self):
        arrayLength = len(self.blurs)
        errorMsg = "Array lengths in non-linear minctracc protocol do not match."
        if (len(self.stepSize) != arrayLength 
            or len(self.iterations) != arrayLength
            or len(self.simplex) != arrayLength
            or len(self.useGradient) != arrayLength
            or len(self.optimization) != arrayLength):
            print errorMsg
            raise
        else:
            return arrayLength
            
    def setParams(self, nlin_protocol):
        """Set parameters from specified protocol"""
        
        """Read parameters into array from csv."""
        inputCsv = open(abspath(nlin_protocol), 'rb')
        csvReader = csv.reader(inputCsv, delimiter=';', skipinitialspace=True)
        params = []
        for r in csvReader:
            params.append(r)
        """initialize arrays """
        self.blurs = []
        self.stepSize = []
        self.iterations = []
        self.simplex = []
        self.useGradient = []
        self.optimization = []

        """Parse through rows and assign appropriate values to each parameter array.
           Everything is read in as strings, but in some cases, must be converted to 
           floats, booleans or gradients. 
        """
        for p in params:
            if p[0]=="blur":
                """Blurs must be converted to floats."""
                for i in range(1,len(p)):
                    self.blurs.append(float(p[i]))
            elif p[0]=="step":
                """Steps are strings but must be converted to a float."""
                for i in range(1,len(p)):
                    self.stepSize.append(float(p[i]))
            elif p[0]=="iterations":
                """The iterations parameter is a string, but must be converted to an int"""
                for i in range(1,len(p)):
                    self.iterations.append(int(p[i]))
            elif p[0]=="simplex":
                """Simplex must be converted to an int."""
                for i in range(1,len(p)):
                    self.simplex.append(int(p[i]))
            elif p[0]=="gradient":
                """Gradients must be converted to bools."""
                for i in range(1,len(p)):
                    if p[i]=="True" or p[i]=="TRUE":
                        self.useGradient.append(True)  
                    elif p[i]=="False" or p[i]=="FALSE":
                        self.useGradient.append(False)          
            elif p[0]=="optimization":
                for i in range(1,len(p)):
                    self.optimization.append(p[i])
            else:
                print "Improper parameter specified for minctracc protocol: " + str(p[0])
                print "Exiting..."
                sys.exit()
        
    def iterate(self):
        for i in range(self.generations):
            """Create file handler for nlin average for each generation"""
            nlinOutput = abspath(self.nlinDir) + "/" + "nlin-%g.mnc" % (i+1)
            nlinFH = RegistrationPipeFH(nlinOutput, mask=self.target.getMask(), basedir=self.nlinDir)
            tblur = blur(self.target, self.blurs[i], gradient=True)              
            self.p.addStage(tblur)
            filesToAvg = []
            for inputFH in self.inputs:
                iblur = blur(inputFH, self.blurs[i], gradient=True)
                self.p.addStage(iblur)
                """If self.useGradient is True, then we call minctracc twice: once
                   with a gradient and once without. Otherwise, we call only once
                   without a gradient. """
                mta = minctracc(inputFH, 
                                self.target, 
                                defaultDir="tmp", 
                                blur=self.blurs[i],
                                gradient=False,
                                iterations=self.iterations[i],
                                step=self.stepSize[i],
                                weight=0.8, 
                                stiffness=0.98,
                                similarity=0.8,
                                simplex=self.simplex[i])
                self.p.addStage(mta)
                if self.useGradient[i]:
                    mtb = minctracc(inputFH, 
                                    self.target, 
                                    defaultDir="tmp", 
                                    blur=self.blurs[i],
                                    gradient=True,
                                    iterations=self.iterations[i],
                                    step=self.stepSize[i],
                                    weight=0.8, 
                                    stiffness=0.98,
                                    similarity=0.8,
                                    simplex=self.simplex[i])
                self.p.addStage(mtb)
                """Need to set last xfm so that next generation will use it as the input transform"""
                inputFH.setLastXfm(nlinFH, mtb.outputFiles[0])
                rs = mincresample(inputFH, self.target, likeFile=self.target, defaultDir="tmp", argArray=["-sinc"])
                #Do we need to resample any masks?
                filesToAvg.append(rs.outputFiles[0])
                self.p.addStage(rs)
                
            """Because we don't reset lastBasevol on each inputFH, call mincAverage with files only.
               File handler has been created above. 
               This solution seems a bit hackish--may want to modify?  
            """
            logBase = removeBaseAndExtension(nlinOutput)
            avgLog = createLogFile(nlinFH.logDir, logBase)
            avg = mincAverage(filesToAvg, nlinOutput, logFile=avgLog)
            self.p.addStage(avg)
            """Reset target for next iteration and add to array"""
            self.target = nlinFH
            self.nlinAvg.append(nlinFH)
        
            
if __name__ == "__main__":
    
    application = NonlinearRegistration()
    application.start()
            
            
            
            