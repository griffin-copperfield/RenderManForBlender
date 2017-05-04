from . import rmanAssets as ra
from . import rmanAssetsLib as ral
from .rmanAssets import RmanAsset, TrMode, TrStorage, TrSpace, TrType
import os
import os.path
import re
import sys
import time
import bpy as mc # just a test
import bpy
from .. import util

##
# @brief      Exception class to tell the world about our miserable failings.
#
class RmanAssetBlenderError(Exception):

    def __init__(self, value):
        self.value = "RmanAssetBlender Error: %s" % value

    def __str__(self):
        return repr(self.value)


##
# @brief      A class used to query environment variables. It can be overriden
#             by a client app, like Maya, who may have a slightly different
#             environment.
#
class BlenderEnv:
    def getenv(self, key):
        util.init_env(None)
        val = os.environ[key]
        return val

    def Exists(self, key):
        # print '++++ MayaEnv::Exists %s' % key
        if self.getenv(key) is not None:
            return True
        else:
            return False

    def GetValue(self, key):
        # print '++++ MayaEnv::GetValue %s' % key
        val = self.getenv(key)
        if val is None:
            raise RmanAssetError('%s is not an registered environment ' +
                                 'variable !' % key)
        # print 'MayaEnv.GetValue( %s ) = %s' % (key, repr(val))
        return os.path.expandvars(val)


# Pass our implementation to override the library's default
#
ra.setEnvClass(BlenderEnv())


blenderEnv = BlenderEnv()
rmantree = ra.internalPath(blenderEnv.GetValue('RMANTREE'))
rmanpath = os.path.join(rmantree, "bin")
if ra.externalPath(rmanpath) not in sys.path:
    sys.path.append(ra.externalPath(rmanpath))

# NOTE: could not make prman module work : needs investigating...
# import prman

##############################
#                            #
#           GLOBALS          #
#                            #
##############################

# store the last read asset to avoid reloading it everytime. Used from MEL.
#
__lastAsset = None

# default categories for our browser
#
__defaultCategories = {'Materials', 'LightRigs', 'EnvironmentMaps'}

# store the list of maya nodes we translate to patterns
# without telling anyone...
#
from .. import nodes
tmp = nodes.nodetypes
g_BlenderToPxrNodes = {}
g_PxrToBlenderNodes = {}

for name, node_class in tmp.items():
    g_BlenderToPxrNodes[name] = node_class.bl_label
    g_PxrToBlenderNodes[node_class.bl_label] = name

# fix material output
g_BlenderToPxrNodes['RendermanOutputNode'] = 'shadingEngine'
g_PxrToBlenderNodes['shadingEngine'] = 'RendermanOutputNode'

# global list of nodes we can translate from a maya DAG
#
g_validNodeTypes = []
# add prman nodes
# classifications = ['rendernode/RenderMan/bxdf',
#                    'rendernode/RenderMan/legacybxdf',
#                    'rendernode/RenderMan/pattern',
#                    'rendernode/RenderMan/legacypattern',
#                    'rendernode/RenderMan/displacementpattern',
#                    'rendernode/RenderMan/exclude',
#                    'rendernode/RenderMan/displacement',
#                    'rendernode/RenderMan/light',
#                    'rendernode/RenderMan/lightfilter',
#                    'rendernode/RenderMan/displayfilter',
#                    'rendernode/RenderMan/samplefilter']
# for cls in classifications:
#     try:
#         g_validNodeTypes += mc.listNodeTypes(cls)
#     except:
#         raise RmanAssetBlenderError('Bad category: "%s"' % cls)
# add maya nodes
g_validNodeTypes += g_BlenderToPxrNodes.keys()
# print 'g_validNodeTypes = %s' % g_validNodeTypes


# wrapper to avoid global access in code
def isValidNodeType(nodetype):
    global g_validNodeTypes
    # if nodetype not in g_validNodeTypes:
    #     print '!! %s is not a valid node type !!' % nodetype
    return (nodetype in g_validNodeTypes)

#
#   END of GLOBALS
#


##
# @brief      Helper to get the last exception message string.
#
# @return     system error message string
#
def sysErr():
    return str(sys.exc_info()[0])


##
# @brief      Returns a normalized maya version, i.e. add a '.0' if it is an
#             integer.
#
# @return     The normalized version string
#
def blenderVersion():
    return bpy.app.version


##
# @brief      Class used by rfm.rmanAssetsLib.renderAssetPreview to report
#             progress back to the host application.
#
class BlenderProgress:
    def __init__(self,):
        self._val = -1
        self._pbar = bpy.context.window_manager
        # print 'Progress init: using %s' % self._pbar

    def Start(self):
        self._pbar.progress_begin(0,100)

    def Update(self, val, msg=None):
        self._pbar.progress_update(val)
        
    def End(self):
        self._pbar.progress_end(val)




##
# @brief    Class representing a node in Maya's DAG
#
#
class BlenderNode:
    __float3 = ['color', 'point', 'vector', 'normal']
    __safeToIgnore = ['Maya_UsePref', 'Maya_Pref']
    __conversionNodeTypes = ['PxrToFloat', 'PxrToFloat3']

    def __init__(self, name, nodetype):
        # the node name / handle
        self.name = name
        # the maya node type
        self.blenderNodeType = nodetype
        # The rman node it translates to. Could be same as mayaNodeType or not.
        self.rmanNodeType = None
        # either 16 or 9 floats (matrix vs. TranslateRotateScale)
        self.transform = []
        self.hasTransform = None
        self.tr_type = None
        self.tr_mode = None
        self.tr_storage = None
        self.tr_space = None
        # 3d manifolds need special treatment
        self.has3dManifold = False
        # node params
        self._params = {}
        self._paramsOrder = []

        # find the corresponding RenderMan node if need be.
        global g_BlenderToPxrNodes
        self.rmanNodeType = self.blenderNodeType
        if self.blenderNodeType in g_BlenderToPxrNodes:
            self.rmanNodeType = g_BlenderToPxrNodes[self.blenderNodeType]

        # special case: osl objects can be injected though the PxrOSL node.
        self.oslPath = None
        if nodetype == 'PxrOSL':
            osl = mc.getAttr(name + '.shadername')
            if not os.path.exists(osl):
                err = ('Cant read osl file: %s' % osl)
                raise RmanAssetBlenderError(err)
            path, fileext = os.path.split(osl)
            self.rmanNodeType = os.path.splitext(fileext)[0]
            self.oslPath = path

        if mc.objExists(self.name):
            self.has3dManifold = mc.objExists('%s.placementMatrix' %
                                              (self.name))
            self.ReadParams()
        else:
            # this will fail if we insert a node that doesn't exist in maya,
            # but it's OK if it is a conversion plug.
            if nodetype not in self.__conversionNodeTypes:
                raise RmanAssetBlenderError(sysErr())
            else:
                self.DefaultParams()

    ##
    # @brief      simple method to make sure we respect the natural parameter
    #             order in our output.
    #
    # @param      self      The object
    # @param      name      The name
    # @param      datadict  The datadict
    #
    # @return     None
    #
    def AddParam(self, name, datadict):
        # print '+ %s : adding %s %s' % (self.name, datadict['type'], name)
        self._paramsOrder.append(name)
        self._params[name] = datadict

    def OrderedParams(self):
        return self._paramsOrder

    def ParamDict(self, name):
        return self._params[name]

    def StoreTransformValues(self, Tnode):
        worldSpace = (self.tr_space == TrSpace.k_world)
        if self.tr_storage == TrStorage.k_TRS:
            # get translate, rotate and scale in world-space and store
            # them in that order in self.transform
            tmp = mc.xform(Tnode, ws=worldSpace, q=True,
                           translation=True)
            self.transform = tmp
            tmp = mc.xform(Tnode, ws=worldSpace, q=True,
                           rotation=True)
            self.transform += tmp
            tmp = mc.xform(Tnode, ws=worldSpace, q=True,
                           scale=True)
            self.transform += tmp
            # print 'k_TRS : %s' % self.transform
        elif self.tr_storage == TrStorage.k_matrix:
            # get the world-space transformation matrix and store
            # in self.transform
            self.transform = mc.xform(Tnode, ws=worldSpace, q=True,
                                      matrix=True)
            # print 'k_matrix: %s' % self.transform

    def HasTransform(self, storage=TrStorage.k_matrix,
                     space=TrSpace.k_world, mode=TrMode.k_flat):
        # we already know and the data has been stored.
        if self.hasTransform is not None:
            return self.hasTransform

        if not mc.objExists(self.name):
            # We may have 'inserted' nodes in our graph that don't exist in
            # maya. 'inserted' nodes are typically color->float3 nodes.
            self.hasTransform = False
            return self.hasTransform

        # get a list of inherited classes for that node
        inherited = mc.nodeType(self.name, inherited=True)

        if 'dagNode' in inherited:
            self.hasTransform = True

            # This is a transform-able node.
            # We store the transformation settings for later use.
            self.tr_mode = mode
            self.tr_storage = storage
            self.tr_space = space

            # we only support flat transformations for now.
            #
            if mode == TrMode.k_flat:
                pass
            elif mode == TrMode.k_hierarchical:
                raise RmanAssetBlenderError('Hierarchical transforms '
                                         'not implemented yet !')
            else:
                raise RmanAssetBlenderError('Unknown transform mode !')

            if 'shape' in inherited:
                transformNodes = mc.listRelatives(self.name, allParents=True,
                                                  type='transform')

                if transformNodes is None:
                    raise RmanAssetBlenderError('This is wrong : '
                                             'no transfom for this shape: %s' %
                                             self.name)

                # print 'we have valid transform nodes : %s' % transformNodes
                Tnode = transformNodes[0]
                # the node is under a transform node
                self.tr_type = TrType.k_nodeTransform
                self.StoreTransformValues(Tnode)
            elif 'transform' in inherited:
                # the node itself is a transform, like place3dTexture...
                self.tr_type = TrType.k_coordsys
                self.StoreTransformValues(self.name)
            else:
                err = 'Unexpected dagNode: %s = %s' % (self.name, inherited)
                raise RmanAssetBlenderError(err)
        else:
            self.hasTransform = False

        return self.hasTransform

    def BlenderGetAttr(self, nodeattr, ptype):
        fail = False
        arraysize = -1

        # get actual parameter value
        pvalue = None
        try:
            pvalue = mc.getAttr(nodeattr)
        except:
            fail = True
            mi = None
            try:
                # could it be an array ? get the array indices.
                mi = mc.getAttr(nodeattr, mi=True)
            except:
                fail = True
            else:
                fail = False

            if mi is not None:
                # this is an array !
                arraysize = len(mi)
                pvalue = mc.getAttr(nodeattr + '[*]')

        if fail:
            # ignore unreadable but warn
            mc.warning("Ignoring un-readable parameter : %s" % nodeattr)
            return None

        if pvalue is None:
            # some maya parameters are not readable unless
            # connected. Remain silent.
            return None

        # some types are returned as [[...]] rather than [...]
        # this is the case for arrays and color, vector, etc.
        if arraysize < 0 and ('[' in ptype or ptype in self.__float3):
            pvalue = pvalue[0]

        # print "   + %s -> %s %s" % (nodeattr, ptype, pvalue)

        return pvalue

    def ReadParams(self):
        # get node parameters
        #
        params = []
        if self.mayaNodeType == 'shadingEngine':
            params = mayaParams(self.mayaNodeType)
        else:
            # This is a rman node
            rmanNode = ra.RmanShadingNode(self.rmanNodeType,
                                          osoPath=self.oslPath)
            params = rmanNode.params()

        # loop through parameters
        #
        for param in params:
            at = param['name']
            ptype = param['type']
            nodeattr = '%s.%s' % (self.name, at)

            # safety check
            if not mc.objExists(nodeattr):
                # sometimes there isn't a one to one correspondence
                # between maya and rman nodes. For example, we use a struct
                # connection to a 'manifold' attr when maya connects 2
                # attrs (uvCoord and uvFilterSize).
                if at == 'manifold':
                    try:
                        c = mc.listConnections('%s.uvCoord' % self.name,
                                               s=True, d=False)
                    except:
                        c = mc.listConnections('%s.placementMatrix' %
                                               self.name,
                                               s=True, d=False)
                    if c is not None:
                        self.AddParam(at, {'type': ptype, 'value': None})
                else:
                    self.AddParam(at, {'type': ptype,
                                       'value': param['default']})
                    if at not in self.__safeToIgnore:
                        mc.warning("Setting missing parameter to default"
                                   " value :" + " %s = %s (%s)" %
                                   (nodeattr, param['default'],
                                    self.mayaNodeType))
                continue

            # store connected params as 'reference type' with
            # None value
            conx = mc.listConnections(nodeattr, s=True, d=False, plugs=True)

            # if the attr is a float3 and one or more components are connected
            # we need to find out.
            if conx is None:
                kidAttrs = None
                # is it a multi (array) ?
                if mc.attributeQuery(at, n=self.name, multi=True):
                    indices = mc.getAttr(nodeattr, mi=True)
                    if indices is not None:
                        kidAttrs = []
                        for i in indices:
                            kidAttrs.append('%s[%d]' % (at, i))
                else:
                    kidAttrs = mc.attributeQuery(at, n=self.name,
                                                 listChildren=True)
                if kidAttrs is not None:
                    for kid in kidAttrs:
                        conx = mc.listConnections('%s.%s' % (self.name, kid),
                                                  s=True, d=False, plugs=True)
                        if conx is not None:
                            break

            if conx is not None and len(conx):
                srcNode = conx[0].split('.')[0]
                if at == 'placementMatrix':
                    # special case !
                    # we don't reference a node connection but we store the
                    # connected node's world inverse matrix
                    manifoldNode = srcNode
                    pvalue = mc.getAttr('%s.wim' % manifoldNode)
                    self.AddParam(at, {'type': ptype, 'value': pvalue})
                else:
                    # connected parameter
                    connectAnyways = (self.mayaNodeType == 'shadingEngine')
                    srcType = mc.nodeType(srcNode)
                    if connectAnyways or isValidNodeType(srcType):
                        self.SetConnected(at, ptype)
                    else:
                        # this could be a random maya node : just read the
                        # src attr value and set it as the parameter's value.
                        # print ('setconnected to alien: %s -> %s (%s)'
                        #        % (at, conx, mc.nodeType(conx[0])))
                        pvalue = self.MayaGetAttr(conx[0], ptype)
                        if pvalue is None:
                            # unreadable : skip
                            continue
                        self.AddParam(at, {'type': ptype, 'value': pvalue})
                        self.AddParamMetadata(at, param)
                continue

            # do not store un-connected input structs
            if ptype[0:6] == 'struct':
                continue

            # get actual parameter value
            pvalue = self.MayaGetAttr(nodeattr, ptype)
            if pvalue is None:
                # unreadable : skip
                continue

            # adjust the array size
            if '[]' in ptype:
                ptype = ptype[:-2]
                arraysize = len(pvalue)
                ptype += '[%d]' % arraysize

            # set basic data
            self.AddParam(at, {'type': ptype, 'value': pvalue})

            # there may be additional data like vstruct stuff...
            self.AddParamMetadata(at, param)

    def DefaultParams(self):
            rmanNode = ra.RmanShadingNode(self.rmanNodeType)
            params = rmanNode.params()
            for p in params:
                self.AddParam(p['name'], {'type': p['type'],
                                          'value': p['default']})

    def SetConnected(self, pname, ptype=None):
        # print 'connected: %s.%s' % (self.name, pname)
        if ptype is None:
            ptype = self._params[pname]['type']
        if 'reference' in ptype:
            return
        self.AddParam(pname, {'type': 'reference %s' % ptype, 'value': None})

    def AddParamMetadata(self, pname, pdict):
        for k, v in pdict.iteritems():
            if k == 'type' or k == 'value':
                continue
            self._params[pname][k] = v

    def __str__(self):
        return ('[[name: %s   mayaNodeType: %s   rmanNodeType: %s]]' %
                (self.name, self.mayaNodeType, self.rmanNodeType))

    def __repr__(self):
        return str(self)


##
# @brief    Represents a Maya shading network.
#
#           Graph nodes will be stored as represented in the maya DAG.
#           The graph analysis will :
#               - store connectivity infos
#               - recognise nodes that need special treatment and process them.
#
#           We need to make sure the final asset is host-agnostic enough to be
#           used in another host. To do so, we decouple the maya DAG from the
#           prman DAG. Effectively, the json file stores RenderMan's graph
#           representation. This class will translate from maya to prman.
#
#           RfM applies special treatment to a number of nodes :
#           - Native maya nodes are translated to PxrMaya* nodes.
#           - Some nodes are ignored (unitConversion, etc).
#           - Some nodes are translated into more than one node.
#               - place3dTexture translates to 2 nodes : PxrMayaPlacement3d and
#                 a scoped coordinate system.
#           - Some connections (float->color, etc) are created by inserting an
#             additionnal node in the graph (PxrToFloat3, PxrToFloat).
#           It is safer to handle these exceptions once the graph has been
#           parsed.
#
class MayaGraph:
    __CompToAttr = {'R': 'inputR', 'X': 'inputR',
                    'G': 'inputG', 'Y': 'inputG',
                    'B': 'inputB', 'Z': 'inputB'}
    __CompToIdx = {'R': 0, 'X': 0,
                   'G': 1, 'Y': 1,
                   'B': 2, 'Z': 2}

    def __init__(self):
        self._nodes = {}
        self._invalids = []
        self._connections = []
        self._extras = {}

    def NodeList(self):
        return self._nodes

    def AddNode(self, nodename, nodetype=None):
        global g_validNodeTypes

        # make sure we always consider the node if we get 'node.attr'
        node = nodename.split('.')[0]
        if nodetype is None:
            nodetype = mc.nodeType(node)
        # print 'MayaGraph.AddNode  >>> %s' % nodename

        if node in self._invalids:
            # print '    already in invalids'
            return False

        if nodetype not in g_validNodeTypes:
            self._invalids.append(node)
            # we must warn the user, as this is not really supposed to happen.
            mc.warning('%s is not a valid node type (%s)' %
                       (nodename, nodetype))
            # print '    not a valid node type -> %s' % nodetype
            return False

        if node not in self._nodes:
            self._nodes[node] = MayaNode(node, nodetype)
            # print '    add to node list'
            return True

        # print '    %s already in node list ? ( %s )' % (node, nodetype)
        return False

    ##
    # @brief      builds topological information and optionaly inserts
    #             floatToFloat3 or float3ToFloat nodes when necessary.
    #
    # @param      self  The object
    #
    # @return     None
    #
    def Process(self):
        global g_validNodeTypes

        # analyse topology
        #
        for node in self._nodes:

            # get incoming connections (both plugs)
            cnx = mc.listConnections(node, s=True, d=False, c=True, p=True)
            # print 'topo: %s -> %s' % (node, cnx)
            if not cnx:
                continue

            for i in range(0, len(cnx), 2):
                srcPlug = cnx[i + 1]
                dstPlug = cnx[i]

                # don't store connections to un-related nodes.
                #
                ignoreDst = mc.nodeType(dstPlug) not in g_validNodeTypes
                ignoreSrc = mc.nodeType(srcPlug) not in g_validNodeTypes
                if ignoreDst or ignoreSrc:
                    continue

                # detect special cases
                #
                srcIsChildPlug = self._isChildPlug(srcPlug)
                dstIsChildPlug = self._isChildPlug(dstPlug)
                # 1: if the connection involves a child plug, we need to insert
                #    one or more conversion node(s).
                #
                if srcIsChildPlug and not dstIsChildPlug:
                    self._f3_to_f1_connection(srcPlug, dstPlug)
                    continue

                elif not srcIsChildPlug and dstIsChildPlug:
                    self._f1_to_f3_connection(srcPlug, dstPlug)
                    continue

                elif srcIsChildPlug and dstIsChildPlug:
                    self._f3_to_f3_connection(srcPlug, dstPlug)
                    continue

                # 2: if a PxrMayaPlacement2d or PxrMayaPlacement3d ...
                #
                srcNodeType = mc.nodeType(srcPlug)
                if srcNodeType in ['place2dTexture', 'place3dTexture']:
                    # store a connection srf.result -> dst.manifold
                    resPlug = '%s.result' % srcPlug.split(".")[0]
                    manPlug = '%s.manifold' % dstPlug.split(".")[0]
                    self._connections.append((resPlug, manPlug))
                    # tag the manifold param as connected
                    self._nodes[node].SetConnected('manifold')

                self._connections.append((srcPlug, dstPlug))

        # remove duplicates
        self._connections = list(set(self._connections))

        # add the extra conversion nodes to the node list
        for k, v in self._extras.iteritems():
            self._nodes[k] = v

    ##
    # @brief      prepare data for the jason file
    #
    # @param      self   The object
    # @param      Asset  The asset
    #
    # @return     None
    #
    def Serialize(self, Asset):
        global g_validNodeTypes

        # register connections
        #
        for srcPlug, dstPlug in self._connections:
            # print '%s -> %s' % (srcPlug, dstPlug)
            Asset.addConnection(srcPlug, dstPlug)

        # register nodes
        #
        for nodeNm, node in self._nodes.iteritems():

            # Add node to asset
            #
            rmanNode = None
            nodeClass = None
            rmanNodeName = node.mayaNodeType
            if node.mayaNodeType == 'shadingEngine':
                nodeClass = 'root'
            else:
                # print 'Serialize %s' % node.mayaNodeType
                rmanNode = ra.RmanShadingNode(node.rmanNodeType,
                                              osoPath=node.oslPath)
                nodeClass = rmanNode.nodeType()
                rmanNodeName = rmanNode.rmanNode()
                # Register the oso file as a dependency that should be saved with
                # the asset.
                if node.oslPath is not None:
                    osoFile = os.path.join(node.oslPath,
                                           '%s.oso' % node.rmanNodeType)
                    Asset.processExternalFile(osoFile)

            Asset.addNode(node.name, node.rmanNodeType,
                          nodeClass, rmanNodeName,
                          externalosl=(node.oslPath is not None))

            # some nodes may have an associated transformation
            # keep it simple for now: we support a single world-space
            # matrix or the TRS values in world-space.
            #
            if node.HasTransform():
                Asset.addNodeTransform(node.name, node.transform,
                                       trStorage=node.tr_storage,
                                       trSpace=node.tr_space,
                                       trMode=node.tr_mode,
                                       trType=node.tr_type)

            for pname in node.OrderedParams():
                Asset.addParam(node.name, pname, node.ParamDict(pname))

            # if the node is a native maya node, add it to the hostNodes
            # compatibility list.
            #
            if node.mayaNodeType != node.rmanNodeType:
                Asset.registerHostNode(node.rmanNodeType)

    def _parentPlug(self, plug):
        tokens = plug.split('.')
        parent = mc.attributeQuery(tokens[-1], node=tokens[0],
                                   listParent=True)
        if parent is None:
            raise RmanAssetBlenderError('%s is not a child plug !')
        tokens[-1] = parent[0]
        return '.'.join(tokens)

    def _isParentPlug(self, plug):
        tokens = plug.split('.')
        parents = mc.attributeQuery(tokens[-1], node=tokens[0],
                                    listParent=True)
        return (parents is None)

    def _isChildPlug(self, plug):
        tokens = plug.split('.')
        parents = mc.attributeQuery(tokens[-1], node=tokens[0],
                                    listParent=True)
        return (parents is not None)

    def _conversionNodeName(self, plug):
        return re.sub('[\W]', '_', plug)

    def _f3_to_f1_connection(self, srcPlug, dstPlug):
        #
        #   Insert a PxrToFloat node:
        #
        #   texture.resultRGBR->srf.presence
        #   becomes:
        #   texture.resultRGB->srf_presence.input|resultF->srf.presence
        #
        convNode = self._conversionNodeName(dstPlug)
        if convNode not in self._extras:
            self._extras[convNode] = MayaNode(convNode,
                                              'PxrToFloat')

        # connect the conversion node's out to the dstPlug's
        # attr
        #
        convOutPlug = '%s.resultF' % convNode
        self._connections.append((convOutPlug, dstPlug))

        # connect the src parent plug to the conversion node's
        # input.
        #
        srcParentPlug = self._parentPlug(srcPlug)
        convInPlug = '%s.input' % (convNode)
        self._connections.append((srcParentPlug, convInPlug))
        # Tell the PxrToFloat node to use the correct channel.
        comp = srcPlug[-1]
        self._extras[convNode]._params['mode']['value'] = \
            self.__CompToIdx[comp]

        # register the connect the plug as connected
        #
        self._extras[convNode].SetConnected('input')

        # print '\noriginal cnx: %s -> %s ---' % (srcPlug, dstPlug)
        # print ('new cnx: %s -> %s | %s -> %s' %
        #        (srcParentPlug, convInPlug, convOutPlug, dstPlug))

    def _f1_to_f3_connection(self, srcPlug, dstPlug):
        #
        #   Insert a PxrToFloat3 node:
        #
        #   noise.resultF->srf.colorR
        #   becomes:
        #   noise.resultF->srf_color.inputR|resultRGB->srf.color
        #
        # create a conversion node
        #
        dstParentPlug = self._parentPlug(dstPlug)
        convNode = self._conversionNodeName(dstParentPlug)
        if convNode not in self._extras:
            self._extras[convNode] = MayaNode(convNode,
                                              'PxrToFloat3')

        # connect the conversion node's out to the dstPlug's
        # parent attr
        #
        convOutPlug = '%s.resultRGB' % convNode
        self._connections.append((convOutPlug, dstParentPlug))

        # connect the src plug to the conversion node's input
        #
        comp = dstPlug[-1]
        convAttr = self.__CompToAttr[comp]
        convInPlug = '%s.%s' % (convNode, convAttr)
        self._connections.append((srcPlug, convInPlug))

        # print '\noriginal cnx: %s -> %s ---' % (srcPlug, dstPlug)
        # print ('new cnx: %s -> %s | %s -> %s' %
        #        (srcPlug, convInPlug, convOutPlug, dstParentPlug))

        # register the conversion plug as connected
        #
        self._extras[convNode].SetConnected(convAttr)

    def _f3_to_f3_connection(self, srcPlug, dstPlug):
        #
        #   Insert a PxrToFloat->PxrToFloat3 node chain:
        #
        #   tex.resultRGBR->srf.colorG
        #   becomes:
        #   tex.resultRGB->srf_colorComp.input|mode=R|resultF->
        #       ->srf_color.inputG|resultRGB->srf.color
        #

        # create PxrToFloat3->dstNode
        parentPlug = self._parentPlug(dstPlug)
        convDst = self._conversionNodeName(parentPlug)
        if convDst not in self._extras:
            self._extras[convDst] = MayaNode(convDst,
                                             'PxrToFloat3')

        # create srcNode->PxrToFloat
        # this time we use the child plug to name the node as each
        # child plug could create a new conversion node.
        convSrc = self._conversionNodeName(srcPlug)
        if convSrc not in self._extras:
            self._extras[convSrc] = MayaNode(convSrc,
                                             'PxrToFloat')

        # connect the srcOutPlug to convSrcInPlug (color->color)
        srcParentPlug = self._parentPlug(srcPlug)
        convSrcInPlug = '%s.input' % convSrc
        self._connections.append((srcParentPlug, convSrcInPlug))
        comp = srcPlug[-1]
        # set the matching channel
        self._extras[convSrc]._params['mode']['value'] = self.__CompToIdx[comp]

        # connect convSrcOutPlug to convDstInPlug
        convSrcOutPlug = '%s.resultF' % convSrc
        comp = dstPlug[-1]
        convDstAttr = self.__CompToAttr[comp]
        convDstInPlug = '%s.%s' % (convDst, convDstAttr)
        self._connections.append((convSrcOutPlug, convDstInPlug))

        # finally, connect convDstOutPlug to dstPlug's parent
        convDstOutPlug = "%s.resultRGB" % convDst
        dstParentPlug = self._parentPlug(dstPlug)
        self._connections.append((convDstOutPlug, dstParentPlug))

        # print '\noriginal cnx: %s -> %s ---' % (srcPlug, dstPlug)
        # print ('new cnx: %s -> %s | %s -> %s | %s -> %s' %
        #        (srcParentPlug, convSrcInPlug, convSrcOutPlug,
        #         convDstInPlug, convDstOutPlug, dstParentPlug))

        # register connected plugs
        self._extras[convSrc].SetConnected('input')
        self._extras[convDst].SetConnected(convDstAttr)

    def __str__(self):
        return ('_nodes = %s\n_connections = %s' %
                (self._nodes, self._connections))

    def __repr__(self):
        return str(self)


##
# @brief      Builds a filename from the asset label string
#
# @param      label  User-friendly label
#
# @return     the asset file name
#
def assetNameFromLabel(label):
    assetDir = re.sub('[^\w]', '', re.sub(' ', '_', label)) + '.rma'
    return assetDir


##
# @brief      Returns the path to the asset library's root directory.
#             The path is stored in a rmanAssetLibrary optionVar. If the
#             optionVar is missing, query the library for the environment var.
#             If it fails too : ask the user to interactively pick a directory.
#
# @return     path as a string
#
def getLibraryPath(pick=False):
    root = ''
    if not pick:
        if mc.optionVar(exists='rmanAssetLibrary'):
            #  We start by checking the rmanAssetLibrary optionVar. If it is
            #  available, this is our first choice.
            root = mc.optionVar(q='rmanAssetLibrary')
            # check if root actually exists
            if os.path.exists(root):
                ral.setLibraryPath(root)
                # print ('getLibraryPath Maya: ',
                #        'root in rmanAssetLibrary optionVar')
                return root

        # next we check is the standard environment variable was used
        try:
            root = ral.getLibraryPath()
        except:
            root = ''
        if root != '':
            mc.optionVar(sv=['rmanAssetLibrary',
                             ral.validateLibraryRoot(root)])
            # print 'getLibraryPath Maya: root defined by lib'
            return root

    if root == '':
        if not mc.about(batch=True):
            # If all failed and we are in an interactive session, ask our user
            # where it should be...
            caption = 'Select the RenderMan Asset Library directory'
            startdir = None
            try:
                # if pick==True, the library path might be valid and it's nicer
                # to jump back to it.
                startdir = ral.getLibraryPath()
            except:
                # If we don't have a valid path, jump to the current project.
                startdir = mc.workspace(q=True, rd=True)

            libdir = mc.fileDialog2(ds=2, fm=3, okc='Select Library',
                                    cap=caption, dir=startdir)
            # print 'libdir = %s' % libdir
            if libdir is not None and os.path.exists(libdir[0]):
                mc.optionVar(sv=['rmanAssetLibrary',
                                 ral.validateLibraryRoot(libdir[0])])
                root = mc.optionVar(q='rmanAssetLibrary')
                ral.setLibraryPath(root)
            else:
                # we raise an exception if the path doesn't exists, but if it
                # was None it means the user pressed the cancel button and we
                # just return an empty string that should be interpreted as
                # 'no update' by the caller.
                if libdir is not None:
                    err = 'RenderMan Asset Library path undefined !!'
                    raise RmanAssetBlenderError(err)
                else:
                    return ''
            # make sure the basic directory structure exists
            root = ral.initLibrary(root)

    if root == '':
        raise RmanAssetBlenderError('RenderMan Asset Library path undefined !!')

    # print 'Library path: %s' % root
    return root


##
# @brief      Return absolute path to an asset's json file.
#
# @param      relpath  relative path to the asset.
#
# @return     absolute json file path (string).
#
def jsonFilePath(relpath):
    # print("jsonFilePath: %s" % (relpath))
    path = ral.getAbsCategoryPath(relpath)
    return os.path.join(path, "asset.json")


##
# @brief      Returns a params array similar to the one returned by
#             rmanShadingNode. This allows us to deal with maya nodes.
#
# @param      nodetype  the maya node type
#
# @return     Array of structs
#
def mayaParams(nodetype):
    params = []
    if nodetype == 'shadingEngine':
        params.append({'type': 'float[]', 'name': 'surfaceShader'})
        params.append({'type': 'float[]', 'name': 'displacementShader'})
        params.append({'type': 'float[]', 'name': 'volumeShader'})
    elif nodetype == 'place3dTexture':
        params.append({'type': 'float[]', 'name': 'translate'})
        params.append({'type': 'float[]', 'name': 'rotate'})
        params.append({'type': 'float[]', 'name': 'scale'})
    else:
        mc.warning('Ignoring unsupported node type: %s !' % nodetype)
    return params


##
# @brief      Parses a Maya node graph, starting from 'node'.
#
# @param      node   root of the graph
# @param      Asset  RmanAsset object to store the nodeGraph
#
# @return     none
#
def parseNodeGraph(nodes, Asset):

    for root in nodes:
        if mc.objExists(root):
            # print ">> %s" % root
            graph = MayaGraph()
            graph.AddNode(root)

            # gather all connected nodes.
            # we get a list of input nodes connected to our root (dst), looking
            # like so : [dst.attr0, src0.attr, dst.attr1, src1.attr, ...]
            #
            tmpc = mc.listConnections(root, source=True, destination=False,
                                      plugs=True)
            upstream = []

            if tmpc and len(tmpc) > 0:
                for plug in tmpc:
                    # make sure we don't catch shape connections on
                    # shading groups
                    if 'instObjGroups' in plug:
                        # print('skip 1: %s' % plug)
                        continue
                    # upstream[] will store all input nodes that need to be
                    # recursively processed.
                    upstream.append(plug)

            # remove duplicate upstream
            upstream = list(set(upstream))

            # Start recursively processing connected nodes.
            # we limit the number of recursions to 10.000 just in case we hit a
            # circular dependency case (bug !)...
            recursions = 0
            _max_recursion = 10000
            while upstream != [] and recursions < _max_recursion:
                # print 'upstream = %s' % upstream

                # this is the first node name in the list of upstream
                this = upstream.pop(0)
                this = this.split('.')[0]

                # return False if already in our list or not a valid node type.
                if graph.AddNode(this) is False:
                    continue

                # get input nodes of this node
                tmpc = mc.listConnections(this, s=True, d=False,
                                          p=True)
                if tmpc is not None:
                    # add list of input nodes to upstream list
                    upstream += tmpc
                    # remove duplicate upstream
                    upstream = list(set(upstream))

                # safety check
                recursions += 1
                if recursions == _max_recursion:
                    raise RmanAssetBlenderError('Reached max recursions (%d)'
                                             ' in graph ! (This is a bug)'
                                             % _max_recursion)

            graph.Process()
            graph.Serialize(Asset)

            # print graph
            # print 'node %s : done' % node
        else:
            raise RmanAssetBlenderError("Can not find node %s" % root)


##
# @brief      Gathers infos from the image header
#
# @param      nodes  the image path
# @param      Asset  The asset in which infos will be stored.
#
def parseTexture(nodes, Asset):
    img = nodes[0]
    # print 'Parsing: %s' % img
    # gather info on the envMap
    #
    Asset.addTextureInfos(img)


##
# @brief      Exports a nodeGraph or envMap as a RenderManAsset
#
# @param      nodes            Maya node used as root
# @param      atype            Asset type : 'nodeGraph' or 'envMap'
# @param      infodict         dict with 'label', 'author' & 'version'
# @param      category         Category as a path, i.e.: "/Lights/LookDev"
# @param      renderPreview    Render an asset preview or not. On by default.
# @param      alwaysOverwrite  default to False. Will ask the user if not in
#                              batch mode.
#
# @return     none
#
def exportAsset(nodes, atype, infodict, category, renderPreview=True,
                alwaysOverwrite=False):
    label = infodict['label']
    Asset = RmanAsset(atype, label)

    # Add user metadata
    #
    for k, v in infodict.iteritems():
        if k == 'label':
            continue
        Asset.addMetadata(k, v)

    # Compatibility data
    # This will help other application decide if they can use this asset.
    #
    prmanVersion = (mc.rman('getversion')).split(' ')[0]
    Asset.setCompatibility(hostName='Maya',
                           hostVersion=mayaVersion(),
                           rendererVersion=prmanVersion)

    # parse maya scene
    #
    if atype is "nodeGraph":
        parseNodeGraph(nodes, Asset)
    elif atype is "envMap":
        parseTexture(nodes, Asset)
    else:
        raise RmanAssetBlenderError("%s is not a known asset type !" % atype)

    #  Get path to our library
    #
    assetPath = ral.getAbsCategoryPath(category)

    #  Create our directory
    #
    assetDir = assetNameFromLabel(label)
    dirPath = os.path.join(assetPath, assetDir)
    if not os.path.exists(dirPath):
        os.mkdir(dirPath)

    #   Check if we are overwriting an existing asset
    #
    jsonfile = os.path.join(dirPath, "asset.json")
    if os.path.exists(jsonfile):
        if mc.about(batch=True) or alwaysOverwrite:
            mc.warning('Replacing existing file : %s' % jsonfile)
        else:
            replace = mc.confirmDialog(title='This file already exists !',
                                       message='Do you want to overwrite it ?',
                                       button=['Overwrite', 'Cancel'],
                                       defaultButton='Replace',
                                       cancelButton='Cancel',
                                       dismissString='Cancel')
            if replace == 'Cancel':
                return

    #  Save our json file
    #
    # print("exportAsset: %s..." %   dirPath)
    Asset.save(jsonfile, False)

    # Render the preview
    #
    if not renderPreview:
        return
    json = Asset.jsonFilePath()
    Asset.load(json, localizeFilePaths=True)
    prog = MayaProgress()
    resizer = MayaResizer()
    if category.startswith('Materials'):
        ral.renderAssetPreview(Asset, progress=prog, resize=resizer)
    elif category.startswith('LightRigs'):
        pass
    elif Asset._type == 'envMap':
        ral.renderAssetPreview(Asset, progress=prog, resize=resizer)


##
# @brief      Sets param values of a nodeGraph node
#
# @param      nodeName    string
# @param      paramsList  list of RmanAssetNodeParam objects
#
# @return     none
#
def setParams(node, paramsList):
    '''Set param values.
       Note: we are only handling a subset of maya attribute types.'''
    float3 = ['color', 'point', 'vector', 'normal']
    for param in paramsList:
        pname = param.name()
        if pname in node.bl_rna.properties.keys():
            ptype = param.type()
            if ptype is None or ptype == 'vstruct':
                # skip vstruct params : they are only useful when connected.
                continue

            pval = param.value()
            if pval is None or pval == []:
                # connected param
                continue

            if pname == "placementMatrix":
                # this param is always connected.
                continue
            # print(' +  set: %s  (%s) -> %s' % (nattr, ptype, pval))
            if 'string' in ptype:
                setattr(node, pname, pval)
            elif ptype in float3:
                try:
                   setattr(node, pname, pval)
                except:
                    print('setParams float3 FAILED: %s  ptype: %s  pval: %s' %
                          (nattr, ptype, repr(pval)))
            else:
                # array parameters are multi attributes in maya.
                if '[' in ptype:
                    setattr(node, pname, pval)
                else:
                    try:
                        setattr(node, pname, pval)
                    except:
                        if type(getattr(node, pname)) == bpy.types.EnumProperty:
                            setattr(node, pname, str(pval))
                        
                        # if ptype == 'riattr':
                        #     mayatype = mc.getAttr(nattr, type=True)
                        #     try:
                        #         mc.setAttr(nattr, pval, type=mayatype)
                        #     except:
                        #         print('setParams scalar FAILED: %s  ptype: %s'
                        #               '  pval:" %s  mayatype: %s' %
                        #               (nattr, ptype, repr(pval), mayatype))
                        # else:
                        #     print('setParams scalar FAILED: %s  ptype: %s'
                        #           '  pval:" %s  mayatype: %s' %
                        #           (nattr, ptype, repr(pval), mayatype))


##
# @brief      Set the transform values of the maya node.
# @note       We only support flat transformations for now, which means that we
#             don't rebuild hierarchies of transforms.
#
# @param      name  The name of the tranform node
# @param      fmt   The format data
# @param      vals  The transformation values
#
# @return     None
#
def setTransform(name, fmt, vals):
    if fmt[2] == TrMode.k_flat:
        if fmt[0] == TrStorage.k_matrix:
            mc.xform(name, ws=True, matrix=vals)
        elif fmt[0] == TrStorage.k_TRS:
            # much simpler
            mc.setAttr((name + '.translate'), *vals[0:3], type='float3')
            mc.setAttr((name + '.rotate'), *vals[3:6], type='float3')
            mc.setAttr((name + '.scale'), *vals[6:9], type='float3')
    else:
        raise RmanAssetBlenderError('Unsupported transform mode ! (hierarchical)')


##
# @brief      Creates all maya nodes defined in the asset's nodeGraph and sets
#             their param values. Nodes will be renamed by Maya and the mapping
#             from original name to actual name retuned as a dict, to allow us
#             to connect the newly created nodes later.
#
# @param      Asset  RmanAsset object containing a nodeGraph
#
# @return     dict mapping the graph id to the actual maya node names.
#
def createNodes(Asset):
    global g_PxrToBlenderNodes
    # preserve selection
    #sel = mc.ls(sl=True)

    nodeDict = {}
    nt = None

    asset_list = Asset.nodeList()
    #first go through and create materials if we need them
    for node in asset_list:
        nodeClass = node.nodeClass()
        if nodeClass == 'root':
            mat = bpy.data.materials.new(Asset.label())
            mat.use_nodes = True
            nt = mat.node_tree


    for node in Asset.nodeList():
        nodeId = node.name()
        nodeType = node.type()
        nodeClass = node.nodeClass()
        print('%s %s: %s' % (nodeId, nodeType, nodeClass))
        fmt, vals, ttype = node.transforms()
        print('+ %s %s: %s' % (fmt, vals, ttype))

    #     nodeName = None
    #     transformName = None

    #     # if nodeType == 'coordinateSystem':
    #     #     # transformed node : custom transform
    #     #     nodeName = mc.createNode('coordinateSystem',
    #     #                              name=nodeId, skipSelect=True)
    #     #     transformName = nodeName
    #     # el
    #     if nodeClass == 'light':
    #         # transformed node : shape under transform
    #         # make sure we return the shape, not the transform name to the
    #         # created node dict.
    #         transformName = mc.shadingNode(nodeType, name=nodeId, asLight=True)
    #         nodeName = mc.listRelatives(transformName, shapes=True)[0]
    #     elif nodeClass == 'lightfilter':
    #         # transformed node : shape under transform
    #         # make sure we return the shape, not the transform name to the
    #         # created node dict.
    #         transformName = mc.shadingNode(nodeType, name=nodeId, asLight=True)
    #         nodeName = mc.listRelatives(transformName, shapes=True)[0]
        if nodeClass == 'bxdf':
            created_node = nt.nodes.new(g_PxrToBlenderNodes[nodeType])
            created_node.name = nodeId
        elif nodeClass == 'pattern':
            if node.externalOSL():
                # if externalOSL() is True, it is a dynamic OSL node i.e. one
                # loaded through a PxrOSL node.
                # if PxrOSL is used, we need to find the oso in the asset to
                # use it in a PxrOSL node.
                oso = Asset.getDependencyPath(nodeType + '.oso')
                if oso is None:
                    err = ('createNodes: OSL file is missing "%s"'
                           % nodeType)
                    raise RmanAssetBlenderError(err)
                created_node = nt.nodes.new(g_PxrToBlenderNodes[nodeType])
                created_node.codetypeswitch = 'EXT'
                created_node.shadercode = oso
            else:
                # the nodeType should in general correspond to a maya node
                # type.
                if nodeType in g_PxrToBlenderNodes:
                    created_node = nt.nodes.new(g_PxrToBlenderNodes[nodeType])
                    created_node.name = nodeId
                else:
                    err = ('createNodes: Unknown nodetype "%s"'
                           % nodeType)
                    raise RmanAssetBlenderError(err)

        elif nodeClass == 'root':
            created_node = nt.nodes.new('RendermanOutputNode')
            created_node.name = nodeId
    #         nodeName = mc.sets(name=nodeId, renderable=True,
    #                            noSurfaceShader=True, empty=True)
    #     else:
    #         nodeName = mc.shadingNode(nodeType, name=nodeId, asUtility=True)
        nodeDict[nodeId] = created_node.name
        setParams(created_node, node.paramsDict())
    #     # print '+ transformName: %s' % (transformName)

    #     if transformName is not None:
    #         setTransform(transformName, fmt, vals)

    # # restore selection
    # mc.select(sel)
    return mat,nt,nodeDict


##
# @brief      Connect all nodes in the nodeGraph. Failed connections are only
#             reported as warning.
#
# @param      Asset     a RmanAssetNode object containg a nodeGraph
# @param      nodeDict  map from graph node name to maya node name. If there
#                       was already a node with the same name as the graph
#                       node, this maps to the new node name.
#
# @return     none
#
def connectNodes(Asset, nt, nodeDict):
    for con in Asset.connectionList():
        #print('+ %s.%s -> %s.%s' % (nodeDict[con.srcNode()](), con.srcParam(),
        #                             nodeDict[con.dstNode()](), con.dstParam()))
        srcNode = nt.nodes[nodeDict[con.srcNode()]]
        dstNode = nt.nodes[nodeDict[con.dstNode()]]
        
        srcSocket = con.srcParam()
        dstSocket = con.dstParam()
        if srcSocket in srcNode.outputs and dstSocket in dstNode.inputs:
            nt.links.new(srcNode.outputs[srcSocket], dstNode.inputs[dstSocket])
        elif dstSocket == 'surfaceShader':
            nt.links.new(srcNode.outputs['Bxdf'], dstNode.inputs['Bxdf'])
        else:
            print('error connecting %s.%s to %s.%s' % (srcNode,srcSocket, dstNode, dstSocket))
        
            # # special cases for maya placement nodes
            # #
            # # print '+ OOOPS: %s' % sysErr()
            # if con.dstParam() in ['manifold', 'uvCoord', 'placementMatrix']:
            #     if mc.objExists('%s.uvCoord' % dstNode):
            #         # this is a node looking for a place2d
            #         src = '%s.outUV' % srcNode
            #         dst = '%s.uvCoord' % dstNode
            #         if mc.isConnected(src, dst):
            #             continue
            #         # print "++ connect %s -> %s" % (src, dst)
            #         mc.connectAttr(src, dst)
            #         src = '%s.outUvFilterSize' % srcNode
            #         dst = '%s.uvFilterSize' % dstNode
            #         # print "++ connect %s -> %s" % (src, dst)
            #         mc.connectAttr(src, dst)
            #     elif mc.objExists('%s.placementMatrix' % dstNode):
            #         # this is a node looking for a place3d
            #         src = '%s.wim[0]' % srcNode
            #         dst = '%s.placementMatrix' % dstNode
            #         if mc.isConnected(src, dst):
            #             continue
            #         # print "++ connect %s -> %s" % (src, dst)
            #         mc.connectAttr(src, dst)
            # else:
            #     mc.warning("Failed to connect : %s -> %s (%s)" %
            #                (src, dst, con.dstParam()))


##
# @brief      Check the compatibility of the loaded asset with the host app and
#             the renderman version. We pass g_validNodeTypes to help determine
#             if we have any substitution nodes available. To support
#             Katana/Blender/Houdini nodes in Maya, you would just need to
#             implement a node with the same name (C++ or OSL) and make it
#             available to RfM.
#
# @param      Asset  The asset we are checking out.
#
# @return     True if compatible, False otherwise.
#
def compatibilityCheck(Asset):
    global g_validNodeTypes
    # the version numbers should always contain at least 1 dot.
    # I'm going to skip the maya stuff
    prmanversion = "%d.%d.%s" % util.get_rman_version(rmantree)
    compatible = Asset.IsCompatible(rendererVersion=prmanversion,
                                    validNodeTypes=g_validNodeTypes)
    if not compatible:
        str1 = 'This Asset is incompatible ! '
        str2 = 'See Script Editor for details...'
        if not mc.about(batch=True):
            mc.confirmDialog(title=str1, message=str2, button=['OK'])
        else:
            print(str1 + str2)
    return compatible


##
# @brief      Import an asset into maya
#
# @param      filepath  full path to a *.rma directory
#
# @return     none
#
def importAsset(filepath):
    # early exit
    if not os.path.exists(filepath):
        raise RmanAssetBlenderError("File doesn't exist: %s" % filepath)

    Asset = RmanAsset()
    Asset.load(filepath, localizeFilePaths=True)
    assetType = Asset.type()

    # compatibility check
    #
    if not compatibilityCheck(Asset):
        return

    if assetType == "nodeGraph":
        mat,nt,newNodes = createNodes(Asset)
        connectNodes(Asset, nt, newNodes)
        return mat

    elif assetType == "envMap":
        scene = bpy.context.scene
        dome_lights = [ob for ob in scene.objects if ob.type == 'LAMP' \
            and ob.data.renderman.renderman_type == 'ENV']

        selected_dome_lights = [ob for ob in dome_lights if ob.select]
        env_map_path = Asset.envMapPath()

        if not selected_dome_lights:
            if not dome_lights:
                # check the world node
                if scene.world.renderman_type == 'ENV':
                    plugin_node = scene.world.renderman.get_light_node()
                    plugin_node.lightColorMap = env_map_path
                # create a new dome light
                else:
                    bpy.ops.object.mr_add_hemi()
                    ob = scene.objects.active
                    plugin_node = ob.data.renderman.get_light_node()
                    plugin_node.lightColorMap = env_map_path

            elif len(dome_lights) == 1:
                lamp = dome_lights[0].data
                plugin_node = lamp.renderman.get_light_node()
                plugin_node.lightColorMap = env_map_path
            else:
                print('More than one dome in scene.  Not sure which to use')
        else:
            for light in selected_dome_lights:
                lamp = dome_lights[0].data
                plugin_node = lamp.renderman.get_light_node()
                plugin_node.lightColorMap = env_map_path


    #     selectedLights = mc.ls(sl=True, dag=True, shapes=True)
    #     # nothing selected ?
    #     if not len(selectedLights):
    #         domeLights = mc.ls(type='PxrDomeLight')
    #         numDomeLights = len(domeLights)
    #         # create a dome light if there isn't already one in the scene !
    #         if numDomeLights == 0:
    #             selectedLights.append(
    #                 mel.eval('rmanCreateNode -asLight "" PxrDomeLight'))
    #         # if there is only one, use that.
    #         elif numDomeLights == 1:
    #             selectedLights.append(domeLights[0])
    #     if len(selectedLights):
    #         envMapPath = Asset.envMapPath()
    #         for light in selectedLights:
    #             nt = mc.nodeType(light)
    #             if nt == 'PxrDomeLight':
    #                 try:
    #                     mc.setAttr('%s.lightColorMap' % light, envMapPath,
    #                                type='string')
    #                 except:
    #                     msg = 'Failed to set %s.lightColorMap\n' % light
    #                     msg += sysErr()
    #                     raise RmanAssetBlenderError(msg)
    #             else:
    #                 raise RmanAssetBlenderError("We only support PxrDomeLight !")
    #     else:
    #         raise RmanAssetBlenderError('Select a PxrDomeLight first !')
    #     # print ("not implemented yet")
    else:
        raise RmanAssetBlenderError("Unknown asset type : %s" % assetType)

    return ''


def setCurrentAsset(category, name):
    global __lastAsset
    __lastAsset = RmanAsset()
    json = jsonFilePath(name)
    # print("- Loading %s" % json)
    __lastAsset.load(json, localizeFilePaths=True)


def currentAssetLabel():
    global __lastAsset
    return __lastAsset.label()


def currentType():
    global __lastAsset
    return __lastAsset.type()


def currentJsonFile():
    global __lastAsset
    return __lastAsset.jsonFilePath()


def currentAssetDir(relative=False):
    global __lastAsset
    if relative is True:
        dirpath = os.path.dirname(__lastAsset.jsonFilePath())
        return dirpath[len(getLibraryPath()):]
    else:
        return os.path.dirname(__lastAsset.jsonFilePath())


def currentInfos():
    global __lastAsset
    infos = __lastAsset.stdMetadata()
    infoStr = ''
    for (k, v) in infos.items():
        if k == 'created':
            infoStr += "%s\n" % (v)
        else:
            infoStr += "%s: %s\n" % (k, v)
    return infoStr


def currentAllMetadata(pretty=False):
    global __lastAsset
    meta = ''
    fmt = '<br><span style="font-weight:bold">%s</span>: %s'
    # print fmt
    for k, v in sorted(__lastAsset._meta.items()):
        if pretty:
            meta += fmt % (k, v)
        else:
            meta += '%s:%s' % (k, v)
    return meta


def currentAllMetadataValues():
    global __lastAsset
    metavals = ''
    for k, v in sorted(__lastAsset._meta.items()):
        metavals += (" %s" % v).lower()
    return metavals


def currentUsedNodes():
    global __lastAsset
    return __lastAsset.getUsedNodeTypes(asString=True)


def currentRIB(jsonfile):
    global __lastAsset
    __lastAsset.load(jsonfile)
    return __lastAsset.getRIB()


def renderPreview():
    global __lastAsset
    prog = MayaProgress()
    ral.renderAssetPreview(__lastAsset, progress=prog, resize=None)
