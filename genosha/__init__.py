#    genosha/__init__.py - GENeral Object marSHAller
#    Copyright (C) 2009 Shawn Sulma <genosha@470th.org>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
r"""GENOSHA (GENeral Object marSHAller) is a library to allow serialization of Python
object graphs.  While :mod:`pickle` performs this task, pickles are not always appropriate, for
example, if a serialized object might need to be manipulated by an outside tool (in the
simplest example: a text editor or shell script).

By itself, the genosha module provides means to represent a set of objects in a
linearly-serializable manner.  Objects of all types (aside from "primitives" such as ints
and bools) are abstracted into GenoshaObjects; references to other objects (represented as
other GenoshaObjects are abstracted into GenoshaReferences. Like :mod:`pickle` there are
limitations to what it is capable of.  The good news is that Genosha handles a few more
situations than pickle.  The limitations are still of a similar kind, however.  The following
types of objects cannot be marshalled using Genosha (not comprehensive, but illustrative):

 - generators (and generator functions);
 - iterators;
 - closures;
 - lambdas;
 - functions and classes that defined dynamically (e.g. references to functions returned
   by functions other than through decoration)
 - old-style classes (mostly because they're going away and they're not really necessary any longer)
 - extension types unless they play very nicely
 - "pathologically-complex" definitions

GenoshaObjects and GenoshaReferences contain the information necessary to reconstruct
the original objects (including references and cycles of references as necessary).

The creation of the serialization structures is performed in memory; the output is not
streamable.

There are two serialization modules provided.  genosha.JSON provides JSON
serialization/deserialization.  genosha.XML provides and XML implementation using ElementTree.
Each of the modules provides an interface that users of :mod:`pickle` should find familiar
(dump, dumps, load, loads).

Usage of the genosha module is relatively straightforward.

    >>> out = genosha.marshal( obj )
    >>> obj_again = genosha.unmarshal( out )

It is probably a good idea to catch TypeErrors, as these are thrown when genosha
encounters a situation it can't handle (e.g. one of the above-listed problematic situations).

Similarly, it is likely you'll want to use either the genosha.JSON or genosha.XML
serializations.  The syntax for those is much the same as for pickle:

    >>> import genosha.JSON
    >>> json_string = genosha.JSON.dumps( obj )
    >>> obj_again = genohsa.JSON.loads( json_string )

    >>> import genosha.XML
    >>> xml_string = genosha.XML.dumps( obj )
    >>> obj_again = genosha.XML.loads( xml_string )

"""
from collections import defaultdict, deque
import sys, types, inspect
try :
    import gc
except : # some python implementations (jython?, pypy?, etc) may not have gc module.  This is okay.
    gc = None

__version__ = "0.1"
__author__ = "Shawn Sulma <genosha@470th.org>"

# special value to indicate the version of genosha object structure used.
SENTINEL = "@genosha:1@"

def marshal ( obj ) :
    r"""Generate a representation of ``obj`` as a list of GenoshaObjects, GenoshaReferences
    and primitives.  The resulting list object will have no cycles in object references and
    can be serialized in whatever manner is appropriate."""
    return GenoshaEncoder().marshal( obj )

def unmarshal ( input ) :
    r"""Convert a representation generated by ``marshal`` back into proper Python objects
    with their references restored.  It assumes that there are no forward-pointing
    GenoshaReferences (i.e. any references will be to objects that have been already
    specified previously in the ``input``."""
    return GenoshaDecoder().unmarshal( input )

class GenoshaObject ( object ) :
    __slots__ = ( 'type', 'oid', 'fields', 'items', 'attribute', 'instance' )
    def __init__ ( self, **kwargs ) :
        for k, v in kwargs.items() :
            setattr( self, k, v )
    def __repr__ ( self ) :
        return "<GenoshaObject:" + ",".join( slot + "=" + str(getattr(self,slot)) for slot in self.__slots__ if hasattr(self,slot) ) + ">"

class GenoshaReference ( object ) :
    __slots__ = ( 'oid', )
    def __init__ ( self, oid ) :
        self.oid = int( oid )
    def __repr__ ( self ) :
        return "<GenoshaReference: oid=%d>" % self.oid

class GenoshaEncoder ( object ) :
    r"""The workhorse for converting an object (and its references) into a serially-marshallable
    structure.  In most cases you will wish to use ``marshal`` above, or one of the
    serialization wrappers (JSON or XML). Instantiating the class directly allows you to
    provide hooks used to customize how the conversion occurs.

    ``object_hook`` refers to the callable used when a GenoshaObject would be created.
    This function should support any of the keyword arguments ( 'type', 'oid', 'fields',
    'items', 'attribute', 'instance' ).  Similarly, the object return by the method should
    accept those same names as attributes.

    ``reference_hook`` refers to the callable used when a GenoshaReference would be
    created.  It should take a single argument for "oid", which represents the (integer)
    internal object reference number.

    ``string_hook`` allows you to specify a string-like-object processor.  If specified
    it should accept the string types (str, unicode) and SHOULD return the same type.
    This is useful for escaping (see the JSON implementation for an example).
    """
    def __init__ ( self, object_hook = GenoshaObject, reference_hook = GenoshaReference, string_hook = None ) :
        self.object_hook = object_hook
        self.reference_hook = reference_hook
        if string_hook :
            self.marshal_str = self.marshal_unicode = self.marshal_basestring = string_hook
            self.primitives -= set( [ str, unicode, basestring ] )
            self.builtin_types |= set( [ str, unicode, basestring ] )

    def marshal ( self, obj ) :
        self.objects = []
        self.oids = set()
        self.python_ids = {}
        self.deferred = deque()
        self.gc = gc and gc.isenabled()
        gc and gc.disable()
        try :
            payload = self._marshal( obj )
            while len( self.deferred ) > 0 :
                self._object( *self.deferred.popleft() )
            return [ SENTINEL, self.objects, payload ]
        finally :
            self.gc and gc.enable()

    def _id ( self, obj ) :
        return self.python_ids.setdefault( id( obj ), len( self.python_ids ) + 1 )

    def _items ( self, obj, klass, iterator, simple = True ) :
        return klass( self._marshal( item ) if simple else ( self._marshal( item[0] ), self._marshal( item[1] ) ) for item in iterator( obj ) )

    def _object ( self, obj, out, items_func, attributes, is_instance ) :
        if items_func is not None :
            out.items = items_func()
        fields = {}
        if hasattr( obj, '__dict__' ) :
            fields.update( item for item in obj.__dict__.items() if ( not item[0].startswith('__') ) and not hasattr( item[1], '__call__' ) )
        elif hasattr( obj, '__slots__' ) :
            fields.update( ( slot, getattr( obj, slot ) ) for slot in obj.__slots__ if (not slot.startswith('__')) and hasattr( obj, slot ) and not hasattr( getattr( obj, slot ), '__call__' ) )
        if attributes :
            fields.update( attributes )
        if is_instance :
            out.fields = self._items( fields, dict, dict.items, simple = False )
        return out

    def marshal_object ( self, obj, items = None, immutable = False, kind = None, attributes = None ) :
        is_instance = not kind
        if not isinstance( kind, basestring ) :
            kind = self.find_scoped_name( kind or obj.__class__ )
        oid = self._id( obj )
        self.oids.add( oid )
        out = self.object_hook( type = kind, oid = oid )
        if immutable :
            self._object( obj, out, items, attributes, is_instance )
        else :
            self.deferred.append( ( obj, out, items, attributes, is_instance ) )
        self.objects.append( out )
        return self.reference_hook( oid )

    def marshal_list ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, list, list.__iter__ ) )

    def marshal_tuple ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, list, tuple.__iter__ ), immutable = True )

    def marshal_dict ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, dict, dict.items, simple = False ) )

    def marshal_set ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, list, set.__iter__ ) )

    def marshal_frozenset ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, list, frozenset.__iter__ ), immutable = True )

    def marshal_defaultdict ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, dict, defaultdict.items, simple = False ), attributes = { 'default_factory' : obj.default_factory } )

    def marshal_deque ( self, obj ) :
        return self.marshal_object( obj, lambda: self._items( obj, list, deque.__iter__, ) )

    def marshal_instancemethod ( self, obj ) :
        oid = self._id( obj )
        self.oids.add( oid )
        out = self.object_hook( oid = oid, instance = self._marshal( obj.im_self ), attribute = obj.im_func.func_name )
        self.objects.append( out )
        return self.reference_hook( oid )

    def marshal_function ( self, obj ) :
        if obj.__name__ == "<lambda>" :
            raise TypeError, "lambdas are not supported."
        if obj.func_closure :
            raise TypeError, "closures are not supported."
        st = self.find_scoped_name( obj )
        if not st :
            if hasattr( obj, 'next' ) and hasattr( getattr( obj, 'next' ), '__call__' ) and hasattr( obj, '__iter__' ) and obj == obj.__iter__() :
                raise TypeError, "iterators are not supported."
            raise TypeError, "function '%s' is not visible in module '%s'. Subscoped functions are not supported." % ( obj.__name__, obj.__module__ )
        return self.marshal_object( obj, kind = st )

    def marshal_type ( self, obj ) :
        return self.marshal_object( obj, kind = obj )

    def marshal_module ( self, obj ) :
        oid = self._id( obj )
        self.oids.add( oid )
        out = self.object_hook( oid = oid, type = obj.__name__ )
        self.objects.append( out )
        return self.reference_hook( oid )

    def marshal_complex ( self, obj ) :
        return self.marshal_object( obj, items = lambda: str( obj )[1:-1] )

    primitives = set( [int, long, float, bool, types.NoneType, unicode, str, basestring] )
    unsupported = set( [ types.GeneratorType, types.InstanceType ] )
    builtin_types = set( [list, tuple, set, frozenset, dict, defaultdict, deque, object, type
        , types.FunctionType, types.MethodType, types.ModuleType, complex ] )

    def _marshal ( self, obj ) :
        if id( obj ) in self.python_ids :
            return self.reference_hook( self._id( obj ) )
        if type( obj ) in self.primitives :
            return obj
        if type( obj ) in self.unsupported :
            raise TypeError, "'%s' is an unsupported type." % type( obj ).__name__
        for kind in inspect.getmro( obj.__class__ ) :
            if kind in self.builtin_types :
                return getattr( self, "marshal_" + kind.__name__ )( obj )
        return self.marshal_object( obj )

    scoping_types = set( [ types.TypeType, types.FunctionType ] )
    def find_scoped_name ( self, obj ) :
        scopes = deque()
        seen = set()
        name = obj.__name__
        scopes.append( ( [], sys.modules[obj.__module__] ) )
        while len( scopes ) > 0 :
            path, scope = scopes.popleft()
            if hasattr( scope, name ) and getattr( scope, name ) is obj :
                path.extend( [ scope.__name__, name ] )
                return "%s/%s" % ( path[0], ".".join( path[1:] ) )
            seen.add( id( scope ) )
            for child in scope.__dict__.values() :
                if type( child ) in self.scoping_types and id( child ) not in seen :
                    scopes.append( ( path + [ scope.__name__ ], child ) )
        raise TypeError, "%s.%s cannot be located in any nested scope. This type is not supported." % ( obj.__module__, obj.__name__ )

class GenoshaDecoder ( object ) :
    r"""Provides the mechanics of converting a genosha-marshalled structure back into
    their proper (original) Python objects. Ordinarily you will want to use the ``unmarshal``
    function instead of instantiating this class directly.  If you want to write your own
    deserializer based on genosha, however, this can be useful.

    ``string_hook`` if specified should identify a callable used to "unescape" any special
    string handling performed during the encode (using ``GenoshaDecode``'s ``string_hook``
    parameter).  See the JSON deserializer for an example of this.
    """
    def __init__ ( self, string_hook = None ) :
        self.string_hook = string_hook

    def unmarshal ( self, obj ) :
        self.objects = {}
        self.to_populate = []
        try :
            if obj[0] != SENTINEL :
                raise ValueError, "Malfomed input."
        except IndexError :
            raise ValueError, "Malformed input."
        self._unmarshal( obj[1:-1] ) # load the referenced objects
        payload = self._unmarshal( obj[-1] )
        for obj in self.to_populate :
            self.populate_object( *obj )
        del self.to_populate
        return payload

    builders = { list : list.extend, set : set.update, dict : dict.update, defaultdict : dict.update, deque : deque.extend }
    immutables = set( [ tuple, frozenset, complex ] )

    def create_object ( self, data ) :
        immediate = not hasattr( data, 'oid' )
        if not hasattr( data, 'attribute' ) :
            kind = self.resolve_type( *data.type.split('/') )
        if hasattr( data, 'attribute' ) :
            obj = getattr( self._unmarshal( data.instance ), data.attribute )
        elif not hasattr( data, 'items' ) and not hasattr( data, 'fields' ) :
            obj = kind # raw type
        elif self.immutables & set ( inspect.getmro( kind ) ) :
            obj = kind.__new__( kind, self._unmarshal( data.items ) )
        else :
            obj = kind.__new__( kind )
            if immediate :
                self.populate_object( obj, data )
            else :
                self.to_populate.append( ( obj, data ) )
        if immediate :
            return obj
        self.objects[int(data.oid)] = obj
        return obj

    def populate_object ( self, obj, data ) :
        if hasattr( data, 'items' ) :
            for base in inspect.getmro( obj.__class__ ) :
                if base in self.builders :
                    self.builders[ base ]( obj, self._unmarshal( data.items ) )
                    break
        if hasattr( data, 'fields' ) :
            if hasattr( obj, '__dict__' ) :
                obj.__dict__.update( self._unmarshal( data.fields ) )
            else :  # __slots__ or descriptor based
                for key, value in data.fields.items() :
                    setattr( obj, self._unmarshal( key ), self._unmarshal( value ) )
        return obj

    def _list ( self, data ) :
        return [ self._unmarshal( item ) for item in data ]
    def _dict ( self, data ) :
        return dict( ( self._unmarshal( key ), self._unmarshal( value ) ) for key, value in data.items() )
    def _object ( self, data ) :
        return self.create_object( data )
    def _reference ( self, data ) :
        try :
            return self.objects[ data.oid ]
        except KeyError :
            raise ValueError, "Forward-references to objects not allowed: " + str( data.oid ) + " (" + str( type( data.oid ) ) + ")"
    def _primitive ( self, data ) :
        return data
    def _string ( self, data ) :
        if self.string_hook :
            return self.string_hook( self, data )
        return data

    dispatch = { list : _list, dict : _dict, GenoshaObject : _object, GenoshaReference : _reference
        , int : _primitive, long : _primitive, float : _primitive, bool : _primitive, types.NoneType : _primitive
        , str : _string, unicode : _string }

    def _unmarshal ( self, data ) :
        return self.dispatch[type(data)]( self, data )

    def resolve_type ( self, modname, kind = '' ) :
        __import__( modname )
        scope = sys.modules[ modname ]
        for name in kind.split('.') :
            scope = getattr( scope, name ) if name else scope
        return scope
